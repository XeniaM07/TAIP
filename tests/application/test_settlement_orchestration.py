"""Quota ACK loss never discards paid work or invents provider attempts."""

from types import SimpleNamespace
from uuid import uuid4
import unittest
from unittest.mock import patch

import fakeredis
import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIProviderError
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.ai_integration.settlement import SettlementCoordinator, SettlementPendingError
from taip.application.runtime import build_runtime
from taip.deployment.core import create_core_app
from taip.deployment.gateway import create_gateway_app
from taip.deployment.redis_limits import RedisRateLimiter
from taip.orchestration.timetable_execution import InlineAIExecution
from tests.ai_integration.test_mock_provider import EXTRACTION_RESPONSE, SOLUTION_RESPONSE, PROMPT
from tests.ai_integration.test_providers import completion
from tests.ai_integration.test_quota_settlement import AmbiguousFinishClient
from tests.deployment.test_gateway import TOKEN, settings


class SettlementOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def stack(self):
        isolation = patch("taip.ai_integration.providers.settlements", SettlementCoordinator())
        isolation.start()
        self.addCleanup(isolation.stop)
        vendor_calls = []

        def vendor(request):
            vendor_calls.append(request)
            raw = EXTRACTION_RESPONSE if len(vendor_calls) == 1 else SOLUTION_RESPONSE
            return httpx.Response(200, json=completion(ProviderKind.GROQ, raw))

        redis = fakeredis.FakeRedis(server=fakeredis.FakeServer())
        self.addCleanup(redis.close)
        backend = AmbiguousFinishClient(redis)
        backend.lose_next_finish_reply = False
        transport = HTTPJSONTransport(transport=httpx.MockTransport(vendor))
        self.addCleanup(transport.close)
        runtime = build_runtime((ProviderConfig(
            "groq", ProviderKind.GROQ, "exact-groq", "synthetic-key-" + uuid4().hex,
        ),), transport=transport,
            limiter=RedisRateLimiter(backend, namespace="taip:test:provider"))
        self.addCleanup(runtime.close)
        core = create_core_app(runtime.workflow, settings(), redis_client=redis,
                               owner=SimpleNamespace(is_current=lambda: True, close=lambda: None))
        core_wire = httpx.ASGITransport(app=core)

        async def upstream(request):
            self.assertEqual(request.url.host, "core")
            return await core_wire.handle_async_request(request)

        gateway = create_gateway_app(
            settings(), limiter=RedisRateLimiter(redis, namespace="taip:test:http"),
            transport=httpx.MockTransport(upstream),
        )
        self.addAsyncCleanup(gateway.aclose)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="https://public.test",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        self.addAsyncCleanup(client.aclose)
        created = await client.post("/api/sessions", json={"prompt": PROMPT})
        self.assertEqual(created.status_code, 201, created.text)
        prefix = "/api/sessions/" + created.json()["session_id"]
        return client, prefix, backend, vendor_calls

    async def test_solution_and_warning_survive_all_layers_and_cached_solve_replay(self):
        client, prefix, backend, vendor_calls = await self.stack()
        self.assertEqual((await client.post(prefix + "/extract")).status_code, 200)
        self.assertEqual((await client.post(prefix + "/models/groq/confirm")).status_code, 200)
        backend.lose_next_finish_reply = True

        solved = await client.post(prefix + "/models/groq/solve?session_view=true")

        self.assertEqual(solved.status_code, 200, solved.text)
        workspace = solved.json()["models"]["groq"]
        self.assertEqual(workspace["status"], "COMPLETED")
        self.assertIsNone(workspace["failure"])
        attempt = workspace["attempts"][-1]
        self.assertTrue(attempt["invocation"]["settlement_pending"])
        self.assertEqual(attempt["invocation"]["settlement_warning"], "quota_settlement_pending")
        self.assertEqual(attempt["invocation"]["outcome"], "success")
        self.assertNotIn("raw_response", attempt)
        for _ in range(3):
            replay = await client.post(prefix + "/models/groq/solve?session_view=true")
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["models"]["groq"]["solution"], workspace["solution"])
            self.assertEqual(replay.json()["models"]["groq"]["attempts"], workspace["attempts"])
        self.assertEqual(len(vendor_calls), 2)
        self.assertEqual(backend.finish_attempts, 2)
        self.assertNotIn("synthetic-key-", solved.text)
        self.assertNotIn("private-password", solved.text)

    async def test_pending_extraction_is_cached_and_blocked_solve_is_not_an_attempt(self):
        client, prefix, backend, vendor_calls = await self.stack()
        backend.lose_next_finish_reply = True
        extracted = await client.post(prefix + "/extract")
        self.assertEqual(extracted.status_code, 200, extracted.text)
        workspace = extracted.json()["models"]["groq"]
        self.assertEqual(workspace["status"], "DRAFT")
        self.assertTrue(workspace["attempts"][0]["invocation"]["settlement_pending"])
        self.assertEqual((await client.post(prefix + "/extract")).status_code, 200)
        self.assertEqual((await client.post(prefix + "/models/groq/confirm")).status_code, 200)
        for _ in range(3):
            denied = await client.post(prefix + "/models/groq/solve?session_view=true")
            self.assertEqual(denied.status_code, 200, denied.text)
            current = denied.json()["models"]["groq"]
            self.assertEqual(current["failure"]["code"], "admission_unavailable")
            self.assertEqual(current["history_counts"]["attempts"], 1)
        self.assertEqual(len(vendor_calls), 1)
        backend.reject_finish = False
        recovered = await client.post(prefix + "/models/groq/solve?session_view=true")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        current = recovered.json()["models"]["groq"]
        self.assertEqual(current["status"], "COMPLETED")
        self.assertEqual(current["history_counts"]["attempts"], 2)
        self.assertFalse(current["attempts"][-1]["invocation"]["settlement_pending"])
        self.assertEqual(len(vendor_calls), 2)


class SettlementAttemptClassificationTests(unittest.TestCase):
    def test_only_known_predispatch_settlement_denials_are_not_provider_attempts(self):
        for error, attempted in (
            (SettlementPendingError(), False),
            (AIProviderError("admission_unavailable"), True),
            (AIProviderError("network_error"), True),
        ):
            with self.subTest(error=type(error).__name__, code=error.code):
                def call():
                    raise error
                outcome = InlineAIExecution().execute("solve", {"model": call})["model"]
                self.assertIs(outcome.exception, error)
                self.assertEqual(outcome.attempted, attempted)
