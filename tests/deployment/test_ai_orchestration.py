"""Real adapters through Redis admission, protected gateway and workflow HTTP."""

from collections import Counter
from types import SimpleNamespace
from threading import Lock
import unittest

import fakeredis
import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.application.runtime import build_runtime
from taip.deployment.core import create_core_app
from taip.deployment.gateway import create_gateway_app
from taip.deployment.redis_limits import RedisRateLimiter
from tests.ai_integration.test_mock_provider import EXTRACTION_RESPONSE, SOLUTION_RESPONSE, PROMPT
from tests.ai_integration.test_providers import completion
from tests.deployment.test_gateway import TOKEN, settings


class ProtectedAIOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def setup_stack(self, *, limited=None):
        counts, lock = Counter(), Lock()
        def provider_wire(request):
            kind = {"generativelanguage.googleapis.com": ProviderKind.GEMINI,
                    "api.groq.com": ProviderKind.GROQ, "openrouter.ai": ProviderKind.OPENROUTER}[request.url.host]
            with lock:
                counts[kind.value] += 1
                count = counts[kind.value]
            if limited == kind.value and count == 2:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json=completion(kind, EXTRACTION_RESPONSE if count == 1 else SOLUTION_RESPONSE))
        redis = fakeredis.FakeRedis()
        self.addCleanup(redis.close)
        transport = HTTPJSONTransport(transport=httpx.MockTransport(provider_wire))
        self.addCleanup(transport.close)
        provider_gate = RedisRateLimiter(redis, namespace="taip:test:providers")
        runtime = build_runtime(tuple(ProviderConfig(kind.value, kind, "exact-" + kind.value, "synthetic-private-" + kind.value)
                                      for kind in ProviderKind), transport=transport, limiter=provider_gate)
        self.addCleanup(runtime.close)
        core = create_core_app(runtime.workflow, settings(), redis_client=redis,
                               owner=SimpleNamespace(is_current=lambda: True, close=lambda: None))
        core_wire = httpx.ASGITransport(app=core)
        async def upstream(request):
            if request.url.host == "core":
                return await core_wire.handle_async_request(request)
            return httpx.Response(200, text="static frontend")
        gate = RedisRateLimiter(redis, namespace="taip:test:http")
        gateway = create_gateway_app(settings(), limiter=gate, transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(gateway.aclose)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="https://public.test",
                                   headers={"Authorization": "Bearer " + TOKEN})
        self.addAsyncCleanup(client.aclose)
        return client, runtime, counts, gate

    async def test_full_protected_three_provider_flow_has_six_generations_and_cached_replays(self):
        client, runtime, counts, gate = await self.setup_stack()
        catalog = (await client.get("/api/runtime")).json()
        self.assertEqual(catalog["mode"], "live")
        self.assertEqual([row["model_id"] for row in catalog["models"]], [kind.value for kind in ProviderKind])
        self.assertNotIn("synthetic-private", str(catalog))
        created = await client.post("/api/sessions", json={"prompt": PROMPT})
        self.assertEqual(created.status_code, 201, created.text)
        prefix = "/api/sessions/" + created.json()["session_id"]
        extracted = await client.post(prefix + "/extract")
        self.assertEqual(extracted.status_code, 200, extracted.text)
        for model in (kind.value for kind in ProviderKind):
            for action in ("confirm", "solve", "verify", "confirm", "solve", "verify"):
                response = await client.post(prefix + "/models/" + model + "/" + action + "?session_view=true")
                self.assertEqual(response.status_code, 200, response.text)
            workspace = response.json()["models"][model]
            self.assertEqual(workspace["verification"]["status"], "verified")
            self.assertEqual(len(workspace["attempts"]), 2)
            self.assertTrue(all("raw_response" not in attempt for attempt in workspace["attempts"]))
        self.assertEqual((await client.post(prefix + "/extract")).status_code, 200)
        self.assertEqual(sum(counts.values()), 6)
        comparison = await client.get(prefix + "/compare")
        self.assertEqual(len(comparison.json()["models"]), 3)
        self.assertTrue(all(runtime.workflow.providers.quota_status(kind.value).requests_in_day == 2 for kind in ProviderKind))
        self.assertEqual(gate.snapshot("global-http-admission").in_flight, 0)

    async def test_provider_rate_limit_survives_all_http_layers_without_an_automatic_retry(self):
        client, runtime, counts, gate = await self.setup_stack(limited="groq")
        session = (await client.post("/api/sessions", json={"prompt": PROMPT})).json()
        prefix = "/api/sessions/" + session["session_id"]
        await client.post(prefix + "/extract")
        await client.post(prefix + "/models/groq/confirm?session_view=true")
        response = await client.post(prefix + "/models/groq/solve?session_view=true")
        self.assertEqual(response.status_code, 200, response.text)
        workspace = response.json()["models"]["groq"]
        self.assertEqual(workspace["status"], "FAILED")
        self.assertEqual(workspace["failure"]["code"], "rate_limit")
        self.assertEqual(workspace["failure"]["retry_after_seconds"], 2)
        self.assertTrue(workspace["failure"]["retryable"])
        self.assertIsNotNone(workspace["snapshot"])
        self.assertEqual(counts["groq"], 2)
        self.assertEqual(gate.snapshot("global-http-admission").in_flight, 0)
