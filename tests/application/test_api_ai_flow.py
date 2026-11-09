"""Exercise real provider adapters and routes over an entirely offline HTTP wire."""

import json
from pathlib import Path
from threading import Lock
import unittest

import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.application.api import create_app
from taip.application.runtime import build_runtime


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "timetable"
EXTRACTION = json.loads((FIXTURES / "extraction_ai1.json").read_text(encoding="utf8"))
SOLUTION = json.loads((FIXTURES / "valid_solution.json").read_text(encoding="utf8"))


class ProviderWire:
    def __init__(self, failures=None):
        self.requests = []
        self.failures = failures or {}
        self.lock = Lock()

    def __call__(self, request):
        body = json.loads(request.content)
        gemini = request.url.host == "generativelanguage.googleapis.com"
        kind = "gemini" if gemini else "groq" if request.url.host == "api.groq.com" else "openrouter"
        user = body["contents"][0]["parts"][0]["text"] if gemini else body["messages"][-1]["content"]
        operation = "extract" if "prompt" in json.loads(user) else "solve"
        with self.lock:
            self.requests.append((kind, operation))
        failure = self.failures.get((kind, operation))
        if failure == "timeout":
            raise httpx.ReadTimeout("private-transport-details", request=request)
        if failure == "rate_limit":
            return httpx.Response(429, headers={"Retry-After": "12"}, json={"error": "private-upstream-response"})
        payload = {"data": EXTRACTION["data"], "missing_information": [], "unsupported_requirements": []} if operation == "extract" else {"assignments": SOLUTION["assignments"]}
        if failure == "invalid_schema":
            payload = {"assignments": [{"response-private-marker": "never-public"}]}
        text = json.dumps(payload)
        envelope = {
            "modelVersion": "returned-gemini",
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 10, "totalTokenCount": 40},
        } if gemini else {
            "model": "returned-" + kind,
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        }
        return httpx.Response(200, json=envelope)


class RealProviderApiTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, wire, kinds=tuple(ProviderKind)):
        transport = HTTPJSONTransport(transport=httpx.MockTransport(wire))
        self.addCleanup(transport.close)
        configs = tuple(ProviderConfig(
            model_id=kind.value, provider=kind, api_model_id="vendor/exact-" + kind.value,
            api_key="fake-never-public-" + kind.value,
        ) for kind in kinds)
        runtime = build_runtime(configs, transport=transport)
        self.addCleanup(runtime.close)
        return runtime

    def client(self, runtime):
        app = create_app(runtime.workflow, runtime_info=runtime.runtime_info)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def test_three_provider_flow_makes_six_requests_and_replays_make_zero(self):
        wire = ProviderWire()
        runtime = self.runtime(wire)
        async with self.client(runtime) as client:
            session_id = (await client.post("/api/sessions", json={"prompt": "Explicit school timetable fixture"})).json()["session_id"]
            extracted = await client.post(f"/api/sessions/{session_id}/extract")
            self.assertEqual(extracted.status_code, 200, extracted.text)
            self.assertEqual(extracted.json()["status"], "DRAFT")
            self.assertEqual(len(wire.requests), 3)
            for model_id in ("gemini", "groq", "openrouter"):
                for action in ("confirm", "solve", "verify"):
                    response = await client.post(f"/api/sessions/{session_id}/models/{model_id}/{action}?session_view=true")
                    self.assertEqual(response.status_code, 200, response.text)
                workspace = response.json()["models"][model_id]
                self.assertEqual(workspace["verification"]["status"], "verified")
                self.assertEqual(len(workspace["attempts"]), 2)
                for attempt in workspace["attempts"]:
                    self.assertNotIn("raw_response", attempt)
                    self.assertNotIn("result", attempt["invocation"])
                    self.assertEqual(attempt["invocation"]["usage"]["total_tokens"], 40)
            self.assertEqual(response.json()["status"], "COMPLETED")
            self.assertEqual(len(wire.requests), 6)
            await client.post(f"/api/sessions/{session_id}/extract")
            for model_id in ("gemini", "groq", "openrouter"):
                for action in ("confirm", "solve", "verify"):
                    response = await client.post(f"/api/sessions/{session_id}/models/{model_id}/{action}?session_view=true")
                    self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(wire.requests), 6)
            compared = await client.get(f"/api/sessions/{session_id}/compare")
            self.assertEqual(len(compared.json()["report"]["rows"]), 3)
            catalog = await client.get("/api/runtime")
            self.assertNotIn("fake-never-public", catalog.text)
            self.assertNotIn("Explicit school timetable fixture", catalog.text)
            self.assertTrue(all(model["quota"]["requests_in_day"] == 2 for model in catalog.json()["models"]))
            self.assertEqual(len(runtime.invocations), 6)

    async def test_partial_provider_failure_is_reconciled_without_automatic_retry(self):
        wire = ProviderWire({("groq", "extract"): "rate_limit"})
        runtime = self.runtime(wire)
        async with self.client(runtime) as client:
            session_id = (await client.post("/api/sessions", json={"prompt": "Explicit fixture"})).json()["session_id"]
            response = await client.post(f"/api/sessions/{session_id}/extract")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "PARTIAL")
            self.assertEqual(response.json()["models"]["gemini"]["status"], "DRAFT")
            failed = response.json()["models"]["groq"]
            self.assertEqual(failed["status"], "FAILED")
            self.assertEqual(failed["failure"]["retry_after_seconds"], 12)
            self.assertEqual(failed["attempts"][0]["invocation"]["http_status_code"], 429)
            self.assertNotIn("private-upstream-response", response.text)
            self.assertEqual(len(wire.requests), 3)
            catalog = (await client.get("/api/runtime")).json()
            self.assertGreater(catalog["models"][1]["quota"]["cooldown_seconds"], 0)
            self.assertTrue(catalog["models"][1]["warnings"])
            compared = await client.get(f"/api/sessions/{session_id}/compare")
            self.assertEqual(compared.status_code, 200)
            self.assertEqual(len(compared.json()["models"]), 3)

    async def test_timeout_and_invalid_output_return_safe_failed_session_views(self):
        for failure in ("timeout", "invalid_schema"):
            with self.subTest(failure=failure):
                wire = ProviderWire({("gemini", "solve"): failure})
                runtime = self.runtime(wire, (ProviderKind.GEMINI,))
                async with self.client(runtime) as client:
                    session_id = (await client.post("/api/sessions", json={"prompt": "Explicit fixture"})).json()["session_id"]
                    await client.post(f"/api/sessions/{session_id}/extract")
                    await client.post(f"/api/sessions/{session_id}/models/gemini/confirm")
                    response = await client.post(f"/api/sessions/{session_id}/models/gemini/solve?session_view=true")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "FAILED")
                self.assertEqual(response.json()["models"]["gemini"]["failure"]["code"], failure)
                self.assertEqual(len(wire.requests), 2)
                self.assertNotIn("response-private-marker", response.text)
                self.assertNotIn("private-transport-details", response.text)


if __name__ == "__main__":
    unittest.main()
