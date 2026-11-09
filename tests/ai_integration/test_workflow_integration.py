"""Lab06 AI providers through the existing session-owning orchestration seam."""

import unittest
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIProviderError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.ai_integration.mock_provider import MockAIProvider
from taip.application.workflow import InMemorySessionRepository, WorkflowService, WorkflowStatus
from taip.benchmark.timetable_service import TimetableBenchmarkService
from taip.orchestration.timetable_execution import TimetableExecutionCoordinator
from taip.common.timetable_contracts import VerificationStatus
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from tests.ai_integration.test_mock_provider import (
    EXTRACTION_RESPONSE, INVALID_SOLUTION_RESPONSE, PROMPT, SOLUTION_RESPONSE,
)
from tests.ai_integration.test_providers import RecordingTransport, completion


class WorkflowAIIntegrationTests(unittest.TestCase):
    def config(self, kind):
        return ProviderConfig(kind.value, kind, "exact-api-model", "fake-never-log-key")

    def roundtrip(self, kind, solution_text):
        replies = [completion(kind, EXTRACTION_RESPONSE), completion(kind, solution_text)]
        records = []
        transport = HTTPJSONTransport(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=replies.pop(0)),
        ))
        self.addCleanup(transport.close)
        factory = AIProviderFactory((self.config(kind),), transport=transport, observer=records.append)
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
        session = service.create_session(PROMPT)
        self.assertEqual(tuple(session.models), (kind.value,))
        service.extract(session.session_id)
        workspace = service.confirm(session.session_id, kind.value)
        snapshot = workspace.snapshot
        service.solve(session.session_id, kind.value)
        self.assertIs(workspace.snapshot, snapshot)
        generated_solution = workspace.solution
        service.verify(session.session_id, kind.value)
        self.assertIs(workspace.solution, generated_solution)
        self.assertEqual(workspace.proposal.problem_id, session.problem.problem_id)
        self.assertEqual(workspace.proposal.model_id, kind.value)
        self.assertEqual([record.operation for record in records], ["extract", "solve"])
        self.assertIs(records[0].result, workspace.proposal)
        self.assertIs(records[1].result, workspace.solution)
        self.assertEqual(records[1].raw_response, solution_text)
        self.assertTrue(all(record.duration_ms >= 0 for record in records))
        self.assertEqual(replies, [])
        return workspace

    def test_three_real_providers_complete_the_full_monitored_workflow_offline(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                workspace = self.roundtrip(kind, SOLUTION_RESPONSE)
                self.assertEqual(workspace.status, WorkflowStatus.COMPLETED)
                self.assertEqual(workspace.verification.status, VerificationStatus.VERIFIED)
                self.assertIsNone(workspace.error)

    def test_generated_invalid_schedule_is_retained_and_independently_rejected(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                workspace = self.roundtrip(kind, INVALID_SOLUTION_RESPONSE)
                self.assertEqual(workspace.status, WorkflowStatus.COMPLETED)
                self.assertEqual(workspace.verification.status, VerificationStatus.FAILED)
                self.assertEqual([lesson.slot_id for lesson in workspace.solution.assignments], ["mon-1", "mon-1"])
                self.assertIn("schedule.teacher_overlap", {issue.code for issue in workspace.verification.violations})

    def test_extraction_timeout_is_monitored_without_stopping_other_models(self):
        records = []
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "api.groq.com":
                raise httpx.ReadTimeout("private network details", request=request)
            kind = ProviderKind.GEMINI if request.url.host == "generativelanguage.googleapis.com" else ProviderKind.OPENROUTER
            return httpx.Response(200, json=completion(kind, EXTRACTION_RESPONSE))

        transport = HTTPJSONTransport(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        factory = AIProviderFactory(tuple(self.config(kind) for kind in ProviderKind), transport=transport, observer=records.append)
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
        session = service.create_session(PROMPT)
        with self.assertLogs("taip.application.workflow", level="ERROR") as logs:
            service.extract(session.session_id)
        self.assertEqual(len(requests), 3)
        self.assertEqual(session.models["gemini"].status, WorkflowStatus.DRAFT)
        self.assertEqual(session.models["openrouter"].status, WorkflowStatus.DRAFT)
        self.assertEqual(session.models["groq"].status, WorkflowStatus.FAILED)
        self.assertEqual([record.outcome for record in records], ["success", "error", "success"])
        self.assertEqual(records[1].error_code, "timeout")
        self.assertNotIn("private network details", "\n".join(logs.output))
        self.assertNotIn("fake-never-log-key", "\n".join(logs.output))

    def test_solving_error_is_monitored_and_propagated_by_orchestration(self):
        records = []
        failure = AIProviderError("rate_limit", status_code=429, retry_after_seconds=2)
        transport = RecordingTransport(completion(ProviderKind.GROQ, EXTRACTION_RESPONSE), failure)
        factory = AIProviderFactory((self.config(ProviderKind.GROQ),), transport=transport, observer=records.append)
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
        session = service.create_session(PROMPT)
        service.extract(session.session_id)
        workspace = service.confirm(session.session_id, "groq")
        with self.assertLogs("taip.application.workflow", level="ERROR"):
            with self.assertRaises(AIProviderError) as caught:
                service.solve(session.session_id, "groq")
        self.assertIs(caught.exception, failure)
        self.assertEqual(workspace.status, WorkflowStatus.FAILED)
        self.assertIsNone(workspace.solution)
        self.assertEqual(records[1].operation, "solve")
        self.assertEqual(records[1].error_code, "rate_limit")
        self.assertEqual(records[1].retry_after_seconds, 2)

    def test_lab06_mock_demo_extracts_solves_and_measures_automatically(self):
        records = []
        provider = MockAIProvider(
            "mock-a", extraction_responses={PROMPT: EXTRACTION_RESPONSE},
            solution_responses={"snapshot-a": SOLUTION_RESPONSE}, observer=records.append,
        )
        factory = SimpleNamespace(MODEL_IDS=("mock-a",), get_provider=lambda model_id: {"mock-a": provider}[model_id])
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
        session = service.create_session(PROMPT)
        with patch("taip.problem_management.service.uuid4", side_effect=["draft-a", "snapshot-a"]):
            service.extract(session.session_id)
            workspace = service.confirm(session.session_id, "mock-a")
        service.solve(session.session_id, "mock-a")
        service.verify(session.session_id, "mock-a")
        self.assertEqual(workspace.verification.status, VerificationStatus.VERIFIED)
        self.assertEqual([record.operation for record in records], ["extract", "solve"])
        self.assertTrue(all(record.provider == "mock" and record.duration_ms >= 0 for record in records))
        self.assertEqual(records[0].raw_response, EXTRACTION_RESPONSE)
        self.assertEqual(records[1].raw_response, SOLUTION_RESPONSE)

    def test_one_three_provider_session_has_six_generations_and_replays_have_none(self):
        requests = []
        lock = Lock()
        counts = {}

        def handler(request):
            kind = {
                "generativelanguage.googleapis.com": ProviderKind.GEMINI,
                "api.groq.com": ProviderKind.GROQ,
                "openrouter.ai": ProviderKind.OPENROUTER,
            }[request.url.host]
            with lock:
                requests.append(request)
                counts[kind] = counts.get(kind, 0) + 1
                number = counts[kind]
            return httpx.Response(200, json=completion(kind, EXTRACTION_RESPONSE if number == 1 else SOLUTION_RESPONSE))

        transport = HTTPJSONTransport(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        coordinator = TimetableExecutionCoordinator()
        self.addCleanup(coordinator.close)
        factory = AIProviderFactory(tuple(self.config(kind) for kind in ProviderKind), transport=transport)
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier(), TimetableBenchmarkService, execution=coordinator)
        session = service.create_session(PROMPT)
        service.extract(session.session_id)
        for model_id in session.models:
            service.confirm(session.session_id, model_id)
            service.solve(session.session_id, model_id)
            service.verify(session.session_id, model_id)
        self.assertEqual(len(requests), 6)
        service.extract(session.session_id)
        for model_id, workspace in session.models.items():
            service.edit_draft(session.session_id, model_id, workspace.draft.data)
            service.confirm(session.session_id, model_id)
            service.solve(session.session_id, model_id)
            service.verify(session.session_id, model_id)
            self.assertEqual(len(workspace.attempts), 2)
            self.assertEqual([item.operation for item in workspace.attempts], ["extract", "solve"])
            self.assertEqual(workspace.attempts[0].raw_response, EXTRACTION_RESPONSE)
            self.assertEqual(workspace.attempts[1].raw_response, SOLUTION_RESPONSE)
            self.assertIsNotNone(workspace.attempts[1].invocation)
        self.assertEqual(len(requests), 6)
        self.assertEqual(len(service.compare(session.session_id).model_runs), 3)

    def test_failed_raw_response_remains_in_first_private_attempt_after_retry(self):
        kind = ProviderKind.GROQ
        transport = RecordingTransport(
            completion(kind, "private malformed response"), completion(kind, EXTRACTION_RESPONSE),
        )
        factory = AIProviderFactory((self.config(kind),), transport=transport)
        service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
        session = service.create_session(PROMPT)
        service.extract(session.session_id)
        workspace = session.models[kind.value]
        first_attempt = workspace.attempts[0]
        self.assertEqual(first_attempt.raw_response, "private malformed response")
        self.assertEqual(first_attempt.failure.code, "invalid_json")
        service.extract(session.session_id)
        self.assertIs(workspace.attempts[0], first_attempt)
        self.assertEqual(len(workspace.attempts), 2)
        self.assertEqual(workspace.status, WorkflowStatus.DRAFT)
        self.assertNotIn("private malformed response", repr(workspace.attempts))


if __name__ == "__main__":
    unittest.main()
