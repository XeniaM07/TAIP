"""Gateway authentication, admission and fixed-upstream isolation, offline."""

import asyncio
import base64
from dataclasses import replace
import hashlib
import unittest

import httpx

from taip.ai_integration.errors import AIProviderError
from taip.ai_integration.rate_limits import RateLimiter
from taip.deployment.settings import DeploymentSettings, RedisSettings


TOKEN = "A" * 64
INTERNAL = "I" * 64


def settings(**changes):
    return DeploymentSettings(public_origin="https://public.test", access_token_digest=hashlib.sha256(TOKEN.encode()).digest(),
                              internal_token=INTERNAL, redis=RedisSettings(password="R" * 64), **changes)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, handler, *, limiter=None, config=None):
        from taip.deployment.gateway import create_gateway_app
        app = create_gateway_app(config or settings(), limiter=limiter or RateLimiter(), transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(app.aclose)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://public.test")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_unauthenticated_requests_never_reach_upstreams(self):
        calls = []
        client = await self.client(lambda request: calls.append(request) or httpx.Response(200))
        response = await client.get("/api/sessions/private")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["www-authenticate"])
        self.assertEqual(calls, [])

    async def test_browser_basic_and_api_bearer_work_without_forwarding_public_credentials(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})
        client = await self.client(handler)
        basic = "Basic " + base64.b64encode(("taip:" + TOKEN).encode()).decode()
        self.assertEqual((await client.get("/", headers={"Authorization": basic})).status_code, 200)
        self.assertEqual(calls[0].url.host, "web")
        self.assertNotIn("authorization", calls[0].headers)
        response = await client.post("/api/sessions", headers={"Authorization": "Bearer " + TOKEN}, json={"prompt": "Data"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[1].url.host, "core")
        self.assertEqual(calls[1].headers["Authorization"], "Bearer " + INTERNAL)
        self.assertNotIn(TOKEN, calls[1].content.decode())

    async def test_ambient_basic_requires_origin_and_forged_forwarding_headers_do_not_help(self):
        calls = []
        client = await self.client(lambda r: calls.append(r) or httpx.Response(200))
        basic = "Basic " + base64.b64encode(("taip:" + TOKEN).encode()).decode()
        for headers in ({"Authorization": basic}, {"Authorization": basic, "Origin": "https://evil.invalid"}):
            self.assertEqual((await client.post("/api/sessions", headers=headers, json={})).status_code, 403)
        response = await client.post("/api/sessions", headers={"Authorization": basic, "Origin": "https://public.test"}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        insecure = httpx.AsyncClient(transport=client._transport, base_url="http://public.test")
        self.addAsyncCleanup(insecure.aclose)
        response = await insecure.get("/", headers={"Authorization": basic, "X-Forwarded-Proto": "https"})
        self.assertEqual(response.status_code, 403)

    async def test_body_limits_and_host_checks_happen_before_forwarding(self):
        calls = []
        client = await self.client(lambda r: calls.append(r) or httpx.Response(200), config=settings(max_body_bytes=32))
        auth = {"Authorization": "Bearer " + TOKEN}
        self.assertEqual((await client.post("/api/sessions", headers=auth, content=b"x" * 33)).status_code, 413)
        self.assertEqual((await client.get("/", headers={**auth, "Host": "evil.invalid"})).status_code, 400)
        self.assertEqual(calls, [])

    async def test_quota_store_failure_is_closed_and_no_request_is_retried(self):
        class Unavailable:
            def acquire(self, *args, **kwargs): raise AIProviderError("admission_unavailable")
            def snapshot(self, key): raise AIProviderError("admission_unavailable")
        calls = []
        client = await self.client(lambda r: calls.append(r) or httpx.Response(200), limiter=Unavailable())
        response = await client.get("/api/health", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])

    async def test_external_redirects_large_responses_and_server_errors_are_not_relayed(self):
        for response in (httpx.Response(302, headers={"Location": "https://evil.invalid"}),
                         httpx.Response(500, content=b"private-error-value"), httpx.Response(200, content=b"x" * 129)):
            calls = []
            client = await self.client(lambda r: calls.append(r) or response, config=settings(max_response_bytes=128))
            actual = await client.get("/api/health", headers={"Authorization": "Bearer " + TOKEN})
            self.assertEqual(actual.status_code, 502)
            self.assertNotIn("private-error-value", actual.text)
            self.assertEqual(len(calls), 1)

    async def test_explicit_tls_termination_supports_private_proxy_connection(self):
        client = await self.client(lambda r: httpx.Response(200, content=b"ok"), config=settings(trust_tls_proxy=True))
        insecure = httpx.AsyncClient(transport=client._transport, base_url="http://public.test")
        self.addAsyncCleanup(insecure.aclose)
        self.assertEqual((await insecure.get("/", headers={"Authorization": "Bearer " + TOKEN})).status_code, 200)

    async def test_internal_health_probe_does_not_require_public_host_or_credentials(self):
        calls = []
        client = await self.client(lambda r: calls.append(r) or httpx.Response(200))
        response = await client.get("/healthz", headers={"Host": "127.0.0.1:8000"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(calls, [])

    async def test_http_quota_is_shared_by_gateway_instances_and_charges_failed_authentication(self):
        gate = RateLimiter()
        config = settings(http_requests_per_minute=2)
        first = await self.client(lambda r: httpx.Response(200), limiter=gate, config=config)
        second = await self.client(lambda r: httpx.Response(200), limiter=gate, config=config)
        self.assertEqual((await first.get("/")).status_code, 401)
        self.assertEqual((await second.get("/")).status_code, 401)
        result = await first.get("/", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(result.status_code, 429)
        self.assertIn("retry-after", result.headers)

    async def test_upstream_has_absolute_deadline_and_no_automatic_retry(self):
        calls = []
        async def slow(request):
            calls.append(request)
            await asyncio.sleep(1)
            return httpx.Response(200)
        client = await self.client(slow, config=settings(request_timeout_seconds=.01))
        result = await client.get("/api/sessions", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(result.status_code, 504)
        self.assertEqual(len(calls), 1)

    async def test_caller_cancellation_holds_permit_until_upstream_finishes(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(request):
            started.set()
            await release.wait()
            return httpx.Response(200, content=b"ok")
        gate = RateLimiter()
        client = await self.client(blocked, limiter=gate, config=settings(http_max_concurrent=1))
        task = asyncio.create_task(client.get("/api/sessions", headers={"Authorization": "Bearer " + TOKEN}))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        result = await client.get("/api/sessions", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(result.status_code, 429)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(gate.snapshot("global-http-admission").in_flight, 0)


if __name__ == "__main__": unittest.main()
