"""Invocation monitoring through real adapters, creators and the offline provider."""

from importlib import import_module, util
import unittest

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIConfigurationError, AIProviderError, AIResponseError
from taip.ai_integration.factory import (
    AIProviderFactory, GeminiTimetableAdapterCreator, GroqTimetableAdapterCreator,
    OpenRouterTimetableAdapterCreator,
)
from taip.ai_integration.mock_provider import MockAIProvider
from taip.ai_integration.providers import GeminiTimetableAdapter, GroqTimetableAdapter, OpenRouterTimetableAdapter
from taip.common.timetable_contracts import OriginalProblem, TimetableData, TimetableProblemSnapshot
from tests.ai_integration.test_providers import RecordingTransport, completion


class MonitoredProviderTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.monitoring"
        self.assertIsNotNone(util.find_spec(name), "AI operation monitoring must exist")
        self.monitoring = import_module(name)

    def config(self, kind):
        return ProviderConfig(kind.value, kind, "requested-exact-model", "fake-secret-key")

    def snapshot(self, kind):
        return TimetableProblemSnapshot("snap", "draft", "problem", kind.value, 1, "Source", TimetableData())

    def test_every_factory_adapter_records_success_and_exact_returned_model(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                records = []
                transport = RecordingTransport(completion(kind))
                factory = AIProviderFactory((self.config(kind),), transport=transport, observer=records.append)
                provider = factory.get_provider(kind.value)
                result = provider.extract(OriginalProblem("problem", "private original prompt"))
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertIs(record.result, result)
                self.assertEqual(record.operation, "extract")
                self.assertEqual(record.provider, kind.value)
                self.assertEqual(record.model_id, kind.value)
                self.assertEqual(record.requested_model_id, "requested-exact-model")
                self.assertEqual(record.returned_model_id, "returned-model-version")
                self.assertEqual(record.raw_response, '{"data":{}}')
                self.assertEqual(record.outcome, "success")
                self.assertIsNone(record.error_code)
                self.assertGreaterEqual(record.duration_ms, 0)

    def test_solving_records_original_invalid_json_assignments_without_correction(self):
        raw = '{"assignments":[{"requirement_id":"missing","occurrence":-1,"slot_id":"x","room_id":"y"}]}'
        for kind in ProviderKind:
            records = []
            transport = RecordingTransport(completion(kind, raw))
            provider = AIProviderFactory((self.config(kind),), transport=transport, observer=records.append).get_provider(kind.value)
            result = provider.solve(self.snapshot(kind))
            self.assertEqual(result.assignments[0].occurrence, -1)
            self.assertEqual(records[0].operation, "solve")
            self.assertEqual(records[0].raw_response, raw)
            self.assertIs(records[0].result, result)

    def test_model_json_error_keeps_raw_text_and_failure_category(self):
        for kind in ProviderKind:
            records = []
            raw = "private malformed JSON"
            provider = AIProviderFactory((self.config(kind),), transport=RecordingTransport(completion(kind, raw)), observer=records.append).get_provider(kind.value)
            with self.assertRaises(AIResponseError):
                provider.extract(OriginalProblem("p", "Source"))
            self.assertEqual(records[0].error_code, "invalid_json")
            self.assertEqual(records[0].outcome, "error")
            self.assertEqual(records[0].raw_response, raw)
            self.assertEqual(records[0].returned_model_id, "returned-model-version")
            self.assertIsNone(records[0].result)
            self.assertNotIn(raw, repr(records[0]))

    def test_transport_error_metadata_is_recorded_and_same_error_is_raised(self):
        records = []
        failure = AIProviderError("rate_limit", status_code=429, retry_after_seconds=3)
        provider = AIProviderFactory((self.config(ProviderKind.GROQ),), transport=RecordingTransport(failure), observer=records.append).get_provider("groq")
        with self.assertRaises(AIProviderError) as caught:
            provider.extract(OriginalProblem("p", "Source"))
        self.assertIs(caught.exception, failure)
        record = records[0]
        self.assertEqual(record.error_code, "rate_limit")
        self.assertEqual(record.http_status_code, 429)
        self.assertEqual(record.retry_after_seconds, 3)
        self.assertIsNone(record.returned_model_id)
        self.assertIsNone(record.raw_response)

    def test_truncated_raw_text_and_model_are_retained_privately(self):
        for kind in ProviderKind:
            records = []
            raw = '{"data":'
            reason = "MAX_TOKENS" if kind is ProviderKind.GEMINI else "length"
            provider = AIProviderFactory((self.config(kind),), transport=RecordingTransport(completion(kind, raw, finish=reason)), observer=records.append).get_provider(kind.value)
            with self.assertRaises(AIProviderError):
                provider.extract(OriginalProblem("p", "Source"))
            self.assertEqual(records[0].error_code, "output_truncated")
            self.assertEqual(records[0].raw_response, raw)
            self.assertEqual(records[0].returned_model_id, "returned-model-version")

    def test_openrouter_records_actual_upstream_provider(self):
        records = []
        envelope = completion(ProviderKind.OPENROUTER)
        envelope["provider"] = "Actual Provider"
        provider = AIProviderFactory((self.config(ProviderKind.OPENROUTER),), transport=RecordingTransport(envelope), observer=records.append).get_provider("openrouter")
        provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(records[0].upstream_provider, "Actual Provider")

    def test_all_concrete_creators_forward_the_optional_observer(self):
        for kind, creator in (
            (ProviderKind.GEMINI, GeminiTimetableAdapterCreator),
            (ProviderKind.GROQ, GroqTimetableAdapterCreator),
            (ProviderKind.OPENROUTER, OpenRouterTimetableAdapterCreator),
        ):
            records = []
            provider = creator(self.config(kind), RecordingTransport(completion(kind)), observer=records.append).create_adapter()
            provider.extract(OriginalProblem("p", "Source"))
            self.assertEqual(len(records), 1)

    def test_observer_is_optional_and_metadata_properties_are_read_only(self):
        adapter = GeminiTimetableAdapter(self.config(ProviderKind.GEMINI), RecordingTransport(completion(ProviderKind.GEMINI)))
        self.assertIsNone(adapter.invocation_observer)
        self.assertEqual(adapter.provider_name, "gemini")
        self.assertEqual(adapter.requested_model_id, "requested-exact-model")
        for name in ("provider_name", "requested_model_id", "invocation_observer"):
            with self.assertRaises(AttributeError):
                setattr(adapter, name, "changed")
        adapter.extract(OriginalProblem("p", "Source"))

    def test_invalid_observer_is_rejected_before_network(self):
        for kind, adapter in (
            (ProviderKind.GEMINI, GeminiTimetableAdapter),
            (ProviderKind.GROQ, GroqTimetableAdapter),
            (ProviderKind.OPENROUTER, OpenRouterTimetableAdapter),
        ):
            with self.assertRaises(AIConfigurationError):
                adapter(self.config(kind), RecordingTransport(), observer="invalid-secret-value")
        with self.assertRaises(AIConfigurationError):
            AIProviderFactory((self.config(ProviderKind.GEMINI),), transport=RecordingTransport(), observer=42)

    def test_mock_extract_and_solve_use_the_same_aop_without_network(self):
        records = []
        provider = MockAIProvider("gemini", extraction_responses={"Source": '{"data":{}}'}, solution_responses={"snap": '{"assignments":[]}'}, observer=records.append)
        extraction = provider.extract(OriginalProblem("problem", "Source"))
        solution = provider.solve(self.snapshot(ProviderKind.GEMINI))
        self.assertEqual([record.operation for record in records], ["extract", "solve"])
        self.assertTrue(all(record.provider == "mock" for record in records))
        self.assertTrue(all(record.requested_model_id == "gemini" for record in records))
        self.assertTrue(all(record.returned_model_id == "gemini" for record in records))
        self.assertIs(records[0].result, extraction)
        self.assertIs(records[1].result, solution)
        self.assertEqual(records[1].raw_response, '{"assignments":[]}')
        self.assertEqual(provider.provider_name, "mock")

    def test_mock_json_error_is_monitored_and_identity_is_checked_before_script_lookup(self):
        records = []
        provider = MockAIProvider("gemini", extraction_responses={"Source": "bad"}, solution_responses={}, observer=records.append)
        with self.assertRaises(AIResponseError):
            provider.extract(OriginalProblem("p", "Source"))
        self.assertEqual(records[0].error_code, "invalid_json")
        self.assertEqual(records[0].raw_response, "bad")
        with self.assertRaises(AIResponseError) as caught:
            provider.solve(self.snapshot(ProviderKind.GROQ))
        self.assertEqual(caught.exception.code, "model_mismatch")
        self.assertEqual(records[1].error_code, "model_mismatch")
        self.assertIsNone(records[1].raw_response)


if __name__ == "__main__":
    unittest.main()
