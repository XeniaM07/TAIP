"""Generation and reported usage metadata through complete provider operations."""

import json
import logging
import unittest

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIProviderError, AIResponseError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.mock_provider import MockAIProvider
from taip.common.timetable_contracts import OriginalProblem, TimetableData, TimetableProblemSnapshot
from tests.ai_integration.demo import run_live_demo
from tests.ai_integration.test_mock_provider import EXTRACTION_RESPONSE, SOLUTION_RESPONSE
from tests.ai_integration.test_providers import RecordingTransport, completion


def with_usage(kind, raw='{"data":{}}', finish=None):
    envelope = completion(kind, raw, finish=finish)
    if kind is ProviderKind.GEMINI:
        envelope["usageMetadata"] = {
            "promptTokenCount": 100, "candidatesTokenCount": 30, "totalTokenCount": 135,
            "thoughtsTokenCount": 5, "cachedContentTokenCount": 10,
        }
    else:
        envelope["usage"] = {
            "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 135,
            "completion_tokens_details": {"reasoning_tokens": 5},
            "prompt_tokens_details": {"cached_tokens": 10},
        }
        if kind is ProviderKind.OPENROUTER:
            envelope["usage"]["cost"] = 0.000021
    return envelope


class InvocationTelemetryTests(unittest.TestCase):
    def provider(self, kind, *envelopes):
        records = []
        config = ProviderConfig(kind.value, kind, "exact-api-model", "fake-private-key",
                                temperature=0.25, max_output_tokens=2048, use_json_schema=True)
        provider = AIProviderFactory((config,), transport=RecordingTransport(*envelopes), observer=records.append).get_provider(kind.value)
        return provider, records

    def assert_usage(self, kind, record):
        self.assertTrue(hasattr(record, "usage"), "Invocation records must retain reported usage")
        usage = record.usage
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 30)
        self.assertEqual(usage.total_tokens, 135)
        self.assertEqual(usage.reasoning_tokens, 5)
        self.assertEqual(usage.cached_input_tokens, 10)
        self.assertEqual(usage.cost_usd, 0.000021 if kind is ProviderKind.OPENROUTER else None)

    def test_both_operations_record_the_actual_requested_generation_parameters(self):
        for kind in ProviderKind:
            provider, records = self.provider(kind, with_usage(kind), with_usage(kind, '{"assignments":[]}'))
            provider.extract(OriginalProblem("p", "Source"))
            provider.solve(TimetableProblemSnapshot("s", "d", "p", kind.value, 1, "Source", TimetableData()))
            for record in records:
                self.assertTrue(hasattr(record, "generation_parameters"), "Generation settings must be observed")
                settings = record.generation_parameters
                self.assertEqual(settings.temperature, 0.25)
                self.assertEqual(settings.max_output_tokens, 2048)
                self.assertIs(settings.use_json_schema, True)

    def test_all_three_provider_usage_formats_are_attached_before_decoding(self):
        for kind in ProviderKind:
            provider, records = self.provider(kind, with_usage(kind))
            provider.extract(OriginalProblem("p", "Source"))
            self.assert_usage(kind, records[0])

    def test_invalid_model_json_keeps_usage_without_changing_the_error(self):
        for kind in ProviderKind:
            provider, records = self.provider(kind, with_usage(kind, "not JSON"))
            with self.assertRaises(AIResponseError):
                provider.extract(OriginalProblem("p", "Source"))
            self.assertEqual(records[0].error_code, "invalid_json")
            self.assert_usage(kind, records[0])

    def test_truncation_keeps_reported_usage_and_request_parameters(self):
        for kind in ProviderKind:
            reason = "MAX_TOKENS" if kind is ProviderKind.GEMINI else "length"
            provider, records = self.provider(kind, with_usage(kind, '{"data":', reason))
            with self.assertRaises(AIProviderError):
                provider.extract(OriginalProblem("p", "Source"))
            self.assertEqual(records[0].error_code, "output_truncated")
            self.assert_usage(kind, records[0])
            self.assertEqual(records[0].generation_parameters.max_output_tokens, 2048)

    def test_network_failure_has_requested_parameters_and_no_invented_usage(self):
        provider, records = self.provider(ProviderKind.GROQ, AIProviderError("timeout"))
        with self.assertRaises(AIProviderError):
            provider.extract(OriginalProblem("p", "Source"))
        self.assertTrue(hasattr(records[0], "generation_parameters"))
        self.assertEqual(records[0].generation_parameters.temperature, 0.25)
        self.assertIsNone(records[0].usage)

    def test_mock_does_not_invent_generation_settings_usage_or_cost(self):
        records = []
        provider = MockAIProvider("mock", extraction_responses={"Source": '{"data":{}}'}, solution_responses={}, observer=records.append)
        provider.extract(OriginalProblem("p", "Source"))
        self.assertTrue(hasattr(records[0], "generation_parameters"))
        self.assertIsNone(records[0].generation_parameters)
        self.assertIsNone(records[0].usage)

    def test_optional_metadata_is_in_safe_logs_and_json_demo_reports(self):
        kind = ProviderKind.OPENROUTER
        provider, records = self.provider(kind, with_usage(kind))
        with self.assertLogs("taip.ai_integration.monitoring", level=logging.INFO) as captured:
            provider.extract(OriginalProblem("p", "private source"))
        event = captured.records[0]
        self.assertEqual(event.ai_generation_parameters["max_output_tokens"], 2048)
        self.assertEqual(event.ai_usage["input_tokens"], 100)
        self.assertNotIn("fake-private-key", repr(vars(event)))
        self.assertNotIn("private source", repr(vars(event)))
        config = ProviderConfig(kind.value, kind, "exact-model", "fake-private-key")
        extraction_payload = json.loads(EXTRACTION_RESPONSE)
        extraction_payload["missing_information"] = []
        extraction_payload["unsupported_requirements"] = []
        report = run_live_demo((config,), transport=RecordingTransport(
            with_usage(kind, json.dumps(extraction_payload)),
            with_usage(kind, SOLUTION_RESPONSE),
        ))
        self.assertEqual(report["models"][0]["status"], "completed")
        self.assertEqual(report["invocations"][0]["usage"]["cost_usd"], 0.000021)
        self.assertEqual(report["invocations"][0]["generation_parameters"]["max_output_tokens"], 8192)
        self.assertNotIn("fake-private-key", json.dumps(report))
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
