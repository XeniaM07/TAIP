"""Admission and input guards before real provider HTTP, with fake time only."""

from dataclasses import replace
import json
import unittest

from taip.ai_integration.config import ProviderConfig, ProviderKind, load_provider_configs
from taip.ai_integration.errors import AIConfigurationError, AIProviderError, AIResponseError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.rate_limits import RateLimiter, RateLimitPolicy
from taip.ai_integration.response_codec import parse_extraction_response
from taip.common.timetable_contracts import (
    ConstraintSeverity, ConstraintType, OriginalProblem, RoomUnavailable,
    TimetableConstraint, TimetableProblemSnapshot,
)
from taip.problem_management.repository import InMemoryProblemRepository
from taip.problem_management.service import TimetableProblemService
from taip.application.workflow import InMemorySessionRepository, WorkflowService, WorkflowStatus
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from tests.ai_integration.test_mock_provider import EXTRACTION_RESPONSE, SOLUTION_RESPONSE, timetable_data
from tests.ai_integration.test_providers import RecordingTransport, completion


class AILimitsIntegrationTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(model_id="groq", provider=ProviderKind.GROQ, api_model_id="exact-model", api_key="fake-private-key",
                      rate_limits=RateLimitPolicy(requests_per_minute=2, requests_per_day=4, tokens_per_minute=1000000))
        values.update(changes)
        return ProviderConfig(**values)

    def test_factory_blocks_before_http_and_exposes_quota_warnings_and_status(self):
        now = [0.0]
        events = []
        limiter = RateLimiter(clock=lambda: now[0], observer=events.append)
        transport = RecordingTransport(*(completion(ProviderKind.GROQ) for _ in range(3)))
        records = []
        factory = AIProviderFactory((self.config(),), transport=transport, limiter=limiter, observer=records.append)
        provider = factory.get_provider("groq")
        for _ in range(2):
            provider.extract(OriginalProblem("p", "Source"))
        with self.assertRaises(AIProviderError) as caught:
            provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(caught.exception.code, "local_rate_limit")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(records[-1].error_code, "local_rate_limit")
        self.assertTrue(any(event.kind == "near_limit" for event in events))
        self.assertNotIn("fake-private-key", repr(events))
        self.assertEqual(factory.quota_status("groq").requests_in_minute, 2)
        now[0] = 60
        provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(len(transport.requests), 3)

    def test_provider_retry_after_cools_down_without_automatic_retry(self):
        now = [0.0]
        transport = RecordingTransport(AIProviderError("rate_limit", status_code=429, retry_after_seconds=3), completion(ProviderKind.GROQ))
        factory = AIProviderFactory((self.config(),), transport=transport, limiter=RateLimiter(clock=lambda: now[0]))
        provider = factory.get_provider("groq")
        with self.assertRaises(AIProviderError) as first:
            provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(first.exception.code, "rate_limit")
        with self.assertRaises(AIProviderError) as second:
            provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(second.exception.code, "local_rate_limit")
        self.assertEqual(len(transport.requests), 1)
        now[0] = 3
        provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(len(transport.requests), 2)

    def test_aliases_sharing_credentials_cannot_bypass_the_same_factory_budget(self):
        transport = RecordingTransport(completion(ProviderKind.GROQ), completion(ProviderKind.GROQ))
        factory = AIProviderFactory((self.config(model_id="a"), self.config(model_id="b")), transport=transport)
        factory.get_provider("a").extract(OriginalProblem("p", "Source"))
        factory.get_provider("b").extract(OriginalProblem("p", "Source"))
        with self.assertRaises(AIProviderError):
            factory.get_provider("b").extract(OriginalProblem("p", "Source"))
        self.assertEqual(len(transport.requests), 2)
        with self.assertRaises(AIConfigurationError):
            AIProviderFactory((self.config(model_id="a"), self.config(model_id="b", rate_limits=RateLimitPolicy())), transport=transport)

    def test_large_source_and_confirmed_workloads_never_reach_http(self):
        transport = RecordingTransport()
        provider = AIProviderFactory((self.config(max_prompt_bytes=16),), transport=transport).get_provider("groq")
        with self.assertRaises(AIResponseError) as caught:
            provider.extract(OriginalProblem("p", "private-marker" * 10))
        self.assertEqual(caught.exception.code, "input_too_large")
        self.assertNotIn("private-marker", str(caught.exception))
        data = timetable_data()
        huge = replace(data, requirements=(replace(data.requirements[0], sessions_per_week=1_000_000_000),))
        with self.assertRaises(AIResponseError) as caught:
            provider.solve(TimetableProblemSnapshot("s", "d", "p", "groq", 1, "source", huge))
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertEqual(transport.requests, [])

    def test_numeric_amplification_in_model_output_stops_before_other_modules(self):
        payload = json.loads(EXTRACTION_RESPONSE)
        payload["data"]["requirements"][0]["sessions_per_week"] = 1_000_000_000
        raw = json.dumps(payload)
        with self.assertRaises(AIResponseError) as caught:
            parse_extraction_response(raw, problem=OriginalProblem("p", "Source"), model_id="groq")
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertEqual(caught.exception.raw_response, raw)

    def test_environment_controls_are_explicit_and_validated(self):
        env = {name: value for prefix in ("GEMINI", "GROQ", "OPENROUTER") for name, value in (
            (prefix + "_API_KEY", "fake-key"), (prefix + "_MODEL", "exact-model"),
        )}
        env |= {"GROQ_REQUESTS_PER_MINUTE": "3", "GROQ_REQUESTS_PER_DAY": "20", "GROQ_TOKENS_PER_MINUTE": "50000",
                "GROQ_MAX_CONCURRENT": "1", "GROQ_WARNING_FRACTION": "0.75", "TAIP_AI_MAX_PROMPT_BYTES": "1024"}
        configs = load_provider_configs(env)
        self.assertEqual(configs[1].rate_limits.requests_per_minute, 3)
        self.assertEqual(configs[1].rate_limits.requests_per_day, 20)
        self.assertEqual(configs[1].rate_limits.tokens_per_minute, 50000)
        self.assertEqual(configs[1].rate_limits.max_concurrent, 1)
        self.assertEqual(configs[1].rate_limits.warning_fraction, 0.75)
        self.assertEqual(configs[0].max_prompt_bytes, 1024)
        with self.assertRaises(AIConfigurationError) as caught:
            load_provider_configs(env | {"GROQ_REQUESTS_PER_MINUTE": "0"})
        self.assertEqual(caught.exception.field, "GROQ_REQUESTS_PER_MINUTE")

    def test_room_unavailability_from_canonical_ai_data_can_be_confirmed(self):
        data = timetable_data()
        constraint = TimetableConstraint("room-block", ConstraintType.ROOM_UNAVAILABLE, ConstraintSeverity.HARD,
                                         RoomUnavailable("room-a", ("tue-1",)))
        from taip.common.timetable_contracts import ExtractionProposal
        manager = TimetableProblemService(InMemoryProblemRepository())
        draft = manager.create_draft(ExtractionProposal("proposal", "problem", "groq", replace(data, constraints=(constraint,))))
        self.assertTrue(manager.validate(draft.draft_id).valid)
        snapshot = manager.confirm(draft.draft_id, "Sala A nu este disponibilă marți.")
        self.assertEqual(snapshot.data.constraints, (constraint,))

    def test_unserializable_confirmed_number_is_a_safe_error_before_http(self):
        transport = RecordingTransport()
        provider = AIProviderFactory((self.config(),), transport=transport).get_provider("groq")
        data = timetable_data()
        huge = replace(data, rooms=(replace(data.rooms[0], capacity=10 ** 5000),))
        with self.assertRaises(AIResponseError) as caught:
            provider.solve(TimetableProblemSnapshot("s", "d", "p", "groq", 1, "Source", huge))
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(transport.requests, [])

    def test_shared_injected_limiter_covers_multiple_factories(self):
        limiter = RateLimiter()
        transport = RecordingTransport(completion(ProviderKind.GROQ), completion(ProviderKind.GROQ))
        first = AIProviderFactory((self.config(),), transport=transport, limiter=limiter)
        second = AIProviderFactory((self.config(),), transport=transport, limiter=limiter)
        first.get_provider("groq").extract(OriginalProblem("p", "Source"))
        second.get_provider("groq").extract(OriginalProblem("p", "Source"))
        with self.assertRaises(AIProviderError):
            first.get_provider("groq").extract(OriginalProblem("p", "Source"))
        self.assertEqual(second.quota_status("groq").requests_in_minute, 2)
        self.assertEqual(len(transport.requests), 2)

    def test_request_budget_rejection_consumes_no_provider_quota(self):
        transport = RecordingTransport()
        factory = AIProviderFactory((self.config(max_request_bytes=100),), transport=transport)
        with self.assertRaises(AIResponseError):
            factory.get_provider("groq").extract(OriginalProblem("p", "Source"))
        self.assertEqual(factory.quota_status("groq").requests_in_minute, 0)
        self.assertEqual(transport.requests, [])

    def test_explicit_solve_retry_after_cooldown_keeps_confirmed_snapshot(self):
        now = [0.0]
        transport = RecordingTransport(completion(ProviderKind.GROQ, EXTRACTION_RESPONSE),
                                       AIProviderError("rate_limit", status_code=429, retry_after_seconds=3),
                                       completion(ProviderKind.GROQ, SOLUTION_RESPONSE))
        factory = AIProviderFactory((self.config(rate_limits=RateLimitPolicy(tokens_per_minute=1000000)),),
                                    transport=transport, limiter=RateLimiter(clock=lambda: now[0]))
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
        session = service.create_session("Controlled test source")
        service.extract(session.session_id)
        workspace = service.confirm(session.session_id, "groq")
        snapshot = workspace.snapshot
        with self.assertLogs("taip.application.workflow", level="ERROR"):
            with self.assertRaises(AIProviderError):
                service.solve(session.session_id, "groq")
        self.assertEqual(workspace.status, WorkflowStatus.FAILED)
        self.assertEqual(len(transport.requests), 2)
        now[0] = 3
        service.solve(session.session_id, "groq")
        self.assertEqual(workspace.status, WorkflowStatus.COMPLETED)
        self.assertIs(workspace.snapshot, snapshot)
        self.assertEqual(len(transport.requests), 3)


if __name__ == "__main__":
    unittest.main()
