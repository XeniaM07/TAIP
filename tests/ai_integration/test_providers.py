"""Offline wire-contract checks for the three real timetable adapters."""

from importlib import import_module, util
import json
import unittest

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIConfigurationError, AIResponseError
from taip.common.timetable_contracts import (
    OriginalProblem, TimetableData, TimetableProblemSnapshot,
)
from taip.common.timetable_protocols import AIProviderPort


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, **request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def completion(kind, text='{"data":{}}', *, finish=None):
    if kind is ProviderKind.GEMINI:
        return {
            "modelVersion": "returned-model-version",
            "candidates": [{"content": {"parts": [{"text": text}]},
                            "finishReason": finish or "STOP"}],
        }
    return {
        "model": "returned-model-version",
        "choices": [{"message": {"content": text}, "finish_reason": finish or "stop"}],
    }


class TimetableProviderTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.providers"
        self.assertIsNotNone(util.find_spec(name), "Real timetable provider adapters must exist")
        self.providers = import_module(name)
        self.error = import_module("taip.ai_integration.errors").AIProviderError

    def config(self, kind, **options):
        return ProviderConfig(
            model_id=kind.value, provider=kind, api_model_id="exact-vendor-model",
            api_key="fake-never-print-secret", **options,
        )

    def adapter(self, kind, transport, **options):
        classes = {
            ProviderKind.GEMINI: self.providers.GeminiTimetableAdapter,
            ProviderKind.GROQ: self.providers.GroqTimetableAdapter,
            ProviderKind.OPENROUTER: self.providers.OpenRouterTimetableAdapter,
        }
        return classes[kind](self.config(kind, **options), transport)

    def snapshot(self, kind):
        return TimetableProblemSnapshot(
            "snapshot-local", "draft-local", "problem-local", kind.value, 2,
            "Text vechi care nu mai este autoritar", TimetableData(),
        )

    def assert_error(self, kind, envelope, code):
        transport = RecordingTransport(envelope)
        with self.assertRaises(self.error) as caught:
            self.adapter(kind, transport).extract(OriginalProblem("problem-local", "Clasa a IX-a"))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("fake-never-print-secret", str(caught.exception))
        return caught.exception

    def test_three_adapters_implement_canonical_port_and_trust_local_identity(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                transport = RecordingTransport(completion(kind))
                adapter = self.adapter(kind, transport)
                self.assertIsInstance(adapter, AIProviderPort)
                result = adapter.extract(OriginalProblem("problem-local", "Clasa a IX-a are matematică."))
                self.assertEqual(result.problem_id, "problem-local")
                self.assertEqual(result.model_id, kind.value)
                self.assertEqual(result.data, TimetableData())
                self.assertEqual(result.raw_response, '{"data":{}}')
                self.assertEqual(adapter.config, self.config(kind))
                with self.assertRaises(AttributeError):
                    adapter.model_id = "changed"

    def test_gemini_request_uses_header_auth_and_current_response_format(self):
        transport = RecordingTransport(completion(ProviderKind.GEMINI))
        adapter = self.adapter(ProviderKind.GEMINI, transport, timeout_seconds=12, max_output_tokens=2048,
                               temperature=0.25, max_response_bytes=4096)
        adapter.extract(OriginalProblem("problem-local", "Elevi români: \"IX A\"\n"))
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://generativelanguage.googleapis.com/v1beta/models/exact-vendor-model:generateContent")
        self.assertNotIn("fake-never-print-secret", request["url"])
        self.assertEqual(request["headers"]["x-goog-api-key"], "fake-never-print-secret")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["timeout_seconds"], 12)
        self.assertEqual(request["max_response_bytes"], 4096)
        body = request["body"]
        generation = body["generationConfig"]
        self.assertEqual(generation["responseFormat"], {"text": {"mimeType": "APPLICATION_JSON"}})
        self.assertEqual(generation["maxOutputTokens"], 2048)
        self.assertEqual(generation["temperature"], 0.25)
        self.assertNotIn("responseMimeType", generation)
        self.assertIn("JSON", body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(json.loads(body["contents"][0]["parts"][0]["text"]), {"prompt": "Elevi români: \"IX A\"\n"})

    def test_chat_requests_pin_exact_model_format_and_disable_openrouter_fallback(self):
        for kind, url in (
            (ProviderKind.GROQ, "https://api.groq.com/openai/v1/chat/completions"),
            (ProviderKind.OPENROUTER, "https://openrouter.ai/api/v1/chat/completions"),
        ):
            with self.subTest(provider=kind):
                transport = RecordingTransport(completion(kind))
                self.adapter(kind, transport).extract(OriginalProblem("p", "Date"))
                request = transport.requests[0]
                self.assertEqual(request["url"], url)
                self.assertEqual(request["headers"]["Authorization"], "Bearer fake-never-print-secret")
                body = request["body"]
                self.assertEqual(body["model"], "exact-vendor-model")
                self.assertIs(body["stream"], False)
                self.assertEqual(body["max_completion_tokens"], 8192)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
                for unwanted in ("models", "tools", "plugins", "max_tokens"):
                    self.assertNotIn(unwanted, body)
                if kind is ProviderKind.OPENROUTER:
                    self.assertEqual(body["provider"], {"allow_fallbacks": False, "require_parameters": True})

    def test_schema_mode_is_explicit_and_never_silently_downgraded(self):
        from taip.ai_integration.response_schema import extraction_json_schema
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                transport = RecordingTransport(completion(kind))
                self.adapter(kind, transport, use_json_schema=True).extract(OriginalProblem("p", "Date"))
                body = transport.requests[0]["body"]
                if kind is ProviderKind.GEMINI:
                    self.assertEqual(body["generationConfig"]["responseFormat"]["text"]["schema"], extraction_json_schema())
                else:
                    shape = body["response_format"]
                    self.assertEqual(shape["type"], "json_schema")
                    self.assertIs(shape["json_schema"]["strict"], True)
                    self.assertEqual(shape["json_schema"]["schema"], extraction_json_schema())

    def test_solve_sends_confirmed_data_and_keeps_invalid_assignments(self):
        raw = '{"assignments":[{"requirement_id":"unknown","occurrence":-1,"slot_id":"unknown","room_id":"unknown"}]}'
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                transport = RecordingTransport(completion(kind, raw))
                result = self.adapter(kind, transport).solve(self.snapshot(kind))
                self.assertEqual(result.snapshot_id, "snapshot-local")
                self.assertEqual(result.model_id, kind.value)
                self.assertEqual(result.assignments[0].occurrence, -1)
                self.assertEqual(result.assignments[0].requirement_id, "unknown")
                body = transport.requests[0]["body"]
                serialized = json.dumps(body, ensure_ascii=False)
                self.assertNotIn("Text vechi", serialized)
                if kind is ProviderKind.GEMINI:
                    user = body["contents"][0]["parts"][0]["text"]
                else:
                    user = body["messages"][1]["content"]
                self.assertEqual(set(json.loads(user)), {"data"})

    def test_foreign_snapshot_is_rejected_before_network(self):
        for kind in ProviderKind:
            transport = RecordingTransport()
            wrong = ProviderKind.GROQ if kind is ProviderKind.GEMINI else ProviderKind.GEMINI
            with self.assertRaises(AIResponseError) as caught:
                self.adapter(kind, transport).solve(self.snapshot(wrong))
            self.assertEqual(caught.exception.code, "model_mismatch")
            self.assertEqual(transport.requests, [])

    def test_provider_mismatch_is_rejected_at_construction(self):
        with self.assertRaises(AIConfigurationError):
            self.providers.GeminiTimetableAdapter(self.config(ProviderKind.GROQ), RecordingTransport())

    def test_gemini_joins_final_text_parts_and_excludes_thoughts(self):
        envelope = completion(ProviderKind.GEMINI)
        envelope["candidates"][0]["content"]["parts"] = [
            {"thought": True, "text": "private reasoning"},
            {"text": '{"data":'}, {"text": '{}}'},
        ]
        result = self.adapter(ProviderKind.GEMINI, RecordingTransport(envelope)).extract(OriginalProblem("p", "Date"))
        self.assertEqual(result.raw_response, '{"data":{}}')

    def test_truncated_responses_are_rejected_even_when_text_is_valid_json(self):
        for kind in ProviderKind:
            reasons = ("MAX_TOKENS", "CONTINUATION") if kind is ProviderKind.GEMINI else ("length",)
            for reason in reasons:
                self.assert_error(kind, completion(kind, finish=reason), "output_truncated")

    def test_blocked_or_refused_outputs_are_reported_without_body_disclosure(self):
        for kind in ProviderKind:
            finish = "SAFETY" if kind is ProviderKind.GEMINI else "content_filter"
            self.assert_error(kind, completion(kind, finish=finish), "provider_refusal")
        self.assert_error(ProviderKind.GEMINI, {"promptFeedback": {"blockReason": "SAFETY"}}, "provider_refusal")
        for kind in (ProviderKind.GROQ, ProviderKind.OPENROUTER):
            envelope = completion(kind)
            envelope["choices"][0]["message"]["refusal"] = "private refusal message"
            error = self.assert_error(kind, envelope, "provider_refusal")
            self.assertNotIn("private refusal", str(error))

    def test_http_200_error_envelopes_and_choice_errors_are_not_successes(self):
        for kind in ProviderKind:
            error = self.assert_error(kind, {"error": {"code": 429, "message": "private upstream text"}}, "rate_limit")
            self.assertNotIn("private upstream", str(error))
        envelope = completion(ProviderKind.OPENROUTER, finish="error")
        envelope["choices"][0]["error"] = {"code": 503, "message": "private"}
        self.assert_error(ProviderKind.OPENROUTER, envelope, "provider_unavailable")

    def test_malformed_missing_or_multiple_candidates_are_rejected(self):
        for kind in ProviderKind:
            valid = completion(kind)
            key = "candidates" if kind is ProviderKind.GEMINI else "choices"
            for candidates in (None, [], "wrong", [None], valid[key] * 2):
                self.assert_error(kind, {key: candidates}, "invalid_envelope")
            unknown = completion(kind, finish="NEW_UNRECOGNIZED_REASON")
            self.assert_error(kind, unknown, "invalid_envelope")

    def test_empty_nontext_and_tool_outputs_are_rejected(self):
        self.assert_error(ProviderKind.GEMINI, {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"functionCall": {"name": "x"}}]}}],
        }, "invalid_envelope")
        for kind in (ProviderKind.GROQ, ProviderKind.OPENROUTER):
            for content in (None, [], {}, "", "   "):
                envelope = completion(kind, content)
                self.assert_error(kind, envelope, "invalid_envelope")
            self.assert_error(kind, completion(kind, finish="tool_calls"), "invalid_envelope")

    def test_model_json_is_decoded_strictly_without_retry_or_repair(self):
        for kind in ProviderKind:
            transport = RecordingTransport(completion(kind, '```json\n{"data":{}}\n```'))
            with self.assertRaises(AIResponseError) as caught:
                self.adapter(kind, transport).extract(OriginalProblem("p", "Date"))
            self.assertEqual(caught.exception.code, "invalid_json")
            self.assertEqual(len(transport.requests), 1)

    def test_transport_failure_is_propagated_unchanged_without_fallback(self):
        for kind in ProviderKind:
            failure = self.error("timeout")
            transport = RecordingTransport(failure)
            with self.assertRaises(self.error) as caught:
                self.adapter(kind, transport).extract(OriginalProblem("p", "Date"))
            self.assertIs(caught.exception, failure)
            self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
