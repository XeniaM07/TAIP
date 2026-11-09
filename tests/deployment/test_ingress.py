"""Short ingress admission has constant state and no authenticated daily budget."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib import util
import asyncio
import multiprocessing
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import fakeredis

from taip.ai_integration.errors import AIConfigurationError, AIProviderError


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(util.find_spec("taip.deployment.ingress"), "Short ingress admission must exist")
        from taip.deployment.ingress import LocalIngressLimiter, RedisIngressLimiter
        self.local_type = LocalIngressLimiter
        self.redis_type = RedisIngressLimiter
        self.now = 1000000.0
        timer = patch("fakeredis.commands_mixins.server_mixin.time.time", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.server = fakeredis.FakeServer()
        self.client = fakeredis.FakeRedis(server=self.server)
        self.addCleanup(self.client.close)

    def assert_denied(self, limiter, expected=60):
        with self.assertRaises(AIProviderError) as caught:
            limiter.check()
        self.assertEqual(caught.exception.code, "local_rate_limit")
        self.assertEqual(caught.exception.retry_after_seconds, expected)

    def test_local_ingress_recovers_after_one_minute(self):
        limiter = self.local_type(requests_per_minute=2, clock=lambda: self.now)
        limiter.check()
        limiter.check()
        self.assert_denied(limiter)
        self.now += 59
        self.assert_denied(limiter, 1)
        self.now += 1
        limiter.check()
        limiter.check()
        self.assert_denied(limiter)

    def test_redis_instances_share_one_short_window_and_constant_bounded_state(self):
        first = self.redis_type(self.client, namespace="taip:test", requests_per_minute=2)
        second = self.redis_type(fakeredis.FakeRedis(server=self.server),
                                 namespace="taip:test", requests_per_minute=2)
        first.check()
        second.check()
        before = self.client.dump("taip:test:ingress")
        for _ in range(100):
            self.assert_denied(first)
            self.assert_denied(second)
        self.assertEqual(self.client.keys(), [b"taip:test:ingress"])
        self.assertEqual(self.client.dump("taip:test:ingress"), before)
        self.now += 59
        self.assert_denied(second, 1)
        self.now += 1
        first.check()
        second.check()
        self.assert_denied(first)

    def test_concurrent_redis_admissions_never_exceed_the_shared_short_limit(self):
        limiters = [self.redis_type(fakeredis.FakeRedis(server=self.server),
                                    namespace="taip:test", requests_per_minute=7) for _ in range(4)]
        def attempt(index):
            try:
                limiters[index % 4].check()
                return True
            except AIProviderError as error:
                self.assertEqual(error.code, "local_rate_limit")
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(attempt, range(40))), 7)
        self.assertEqual(len(self.client.keys()), 1)

    def test_redis_ingress_policy_mismatch_and_backend_failure_are_closed(self):
        first = self.redis_type(self.client, namespace="taip:test", requests_per_minute=2)
        first.check()
        other = self.redis_type(self.client, namespace="taip:test", requests_per_minute=3)
        with self.assertRaises(AIProviderError) as caught:
            other.check()
        self.assertEqual(caught.exception.code, "admission_unavailable")
        self.server.connected = False
        with self.assertRaises(AIProviderError) as caught:
            first.check()
        self.assertEqual(caught.exception.code, "admission_unavailable")

    def test_invalid_redis_counter_metadata_cannot_reset_the_shared_window(self):
        limiter = self.redis_type(self.client, namespace="taip:test", requests_per_minute=2)
        self.client.hset("taip:test:ingress", mapping={"count": "invalid", "limit": "invalid", "until": "invalid"})
        with self.assertRaises(AIProviderError) as caught:
            limiter.check()
        self.assertEqual(caught.exception.code, "admission_unavailable")

    def test_invalid_limits_and_backend_responses_are_closed(self):
        for value in (0, -1, True, 1.0, 1000001):
            with self.subTest(value=value), self.assertRaises(AIConfigurationError):
                self.local_type(requests_per_minute=value)
            with self.subTest(value=value), self.assertRaises(AIConfigurationError):
                self.redis_type(self.client, requests_per_minute=value)
        for response in (None, [True, 0], [1, 1], [0, 0], [0, 60001], [-1, 0], [1]):
            with self.subTest(response=response):
                limiter = self.redis_type(self.client)
                with patch.object(self.client, "eval", return_value=response):
                    with self.assertRaises(AIProviderError) as caught:
                        limiter.check()
                self.assertEqual(caught.exception.code, "admission_unavailable")


def _ingress_worker(url, namespace, ready, start, result):
    from taip.deployment.ingress import RedisIngressLimiter
    from tests.deployment.test_redis_processes import _client
    client = None
    try:
        client = _client(url)
        limiter = RedisIngressLimiter(client, namespace=namespace, requests_per_minute=7)
        ready.put("ready")
        if not start.wait(15):
            raise TimeoutError
        admitted = 0
        for _ in range(10):
            try:
                limiter.check()
                admitted += 1
            except AIProviderError as error:
                if error.code != "local_rate_limit":
                    raise
        result.put(admitted)
    except Exception as error:
        result.put(type(error).__name__)
    finally:
        if client is not None:
            client.close()


class RealRedisIngressTests(unittest.TestCase):
    """CI opt-in contracts; only a fresh test namespace is read or deleted."""

    @classmethod
    def setUpClass(cls):
        cls.redis_url = os.environ.get("TAIP_TEST_REDIS_URL")
        if not cls.redis_url:
            raise unittest.SkipTest("TAIP_TEST_REDIS_URL is not configured")

    def setUp(self):
        from tests.deployment.test_redis_processes import _client, _safe_redis_call
        self.namespace = "taip:test:" + uuid4().hex
        self.client = _client(self.redis_url)
        self.addCleanup(self.client.close)
        self.addCleanup(self._delete_namespace)
        _safe_redis_call("Redis ingress integration ping", self.client.ping)

    def _delete_namespace(self):
        from tests.deployment.test_redis_processes import _safe_redis_call
        def delete():
            prefix = (self.namespace + ":").encode("ascii")
            keys = list(self.client.scan_iter(match=prefix + b"*", count=128))
            if any(not key.startswith(prefix) for key in keys):
                raise AssertionError("Integration cleanup escaped its namespace.")
            if keys:
                self.client.delete(*keys)
        _safe_redis_call("Redis ingress namespace cleanup", delete)

    def test_real_redis_ingress_is_shared_by_spawned_processes_with_one_key(self):
        context = multiprocessing.get_context("spawn")
        ready, result = context.Queue(), context.Queue()
        self.addCleanup(ready.close)
        self.addCleanup(result.close)
        start = context.Event()
        processes = [context.Process(target=_ingress_worker,
                      args=(self.redis_url, self.namespace, ready, start, result)) for _ in range(3)]
        try:
            for process in processes:
                process.start()
            self.assertEqual([ready.get(timeout=15) for _ in processes], ["ready"] * 3)
            start.set()
            counts = [result.get(timeout=15) for _ in processes]
            self.assertTrue(all(type(count) is int for count in counts), counts)
            self.assertEqual(sum(counts), 7)
            self.assertEqual(list(self.client.scan_iter(match=self.namespace + ":*")),
                             [(self.namespace + ":ingress").encode()])
            self.assertGreater(self.client.pttl(self.namespace + ":ingress"), 0)
            self.assertLessEqual(self.client.pttl(self.namespace + ":ingress"), 60000)
        finally:
            start.set()
            for process in processes:
                if process.pid is not None:
                    process.join(timeout=3)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=3)
                    process.close()

    def test_real_gateway_workers_keep_anonymous_and_origin_rejections_out_of_daily_quota(self):
        async def verify():
            import httpx
            from taip.deployment.gateway import create_gateway_app
            from tests.deployment.test_gateway import settings, TOKEN
            from tests.deployment.test_redis_processes import _client
            from taip.deployment.redis_limits import RedisRateLimiter
            config = settings(http_requests_per_day=2)
            config = replace(config, redis=replace(config.redis, namespace=self.namespace))
            backend = _client(self.redis_url)
            first = create_gateway_app(config, redis_client=self.client,
                                       transport=httpx.MockTransport(lambda r: httpx.Response(200)))
            second = create_gateway_app(config, redis_client=backend,
                                        transport=httpx.MockTransport(lambda r: httpx.Response(200)))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first),
                                              base_url="https://public.test") as a, \
                           httpx.AsyncClient(transport=httpx.ASGITransport(app=second),
                                              base_url="https://public.test") as b:
                    self.assertEqual((await a.get("/")).status_code, 401)
                    self.assertEqual((await b.get("/")).status_code, 401)
                    headers = {"Authorization": "Bearer " + TOKEN}
                    rejected = await a.post("/api/sessions", headers={**headers, "Origin": "null"}, json={})
                    self.assertEqual(rejected.status_code, 403)
                    quota = RedisRateLimiter(self.client, namespace=self.namespace + ":http")
                    self.assertEqual(quota.snapshot("global-http-admission").requests_in_day, 0)
                    self.assertEqual((await a.get("/", headers=headers)).status_code, 200)
                    self.assertEqual((await b.get("/", headers=headers)).status_code, 200)
                    self.assertEqual(quota.snapshot("global-http-admission").requests_in_day, 2)
                    denied = await a.get("/", headers=headers)
                    self.assertEqual(denied.status_code, 429)
                    self.assertGreaterEqual(int(denied.headers["retry-after"]), 86390)
            finally:
                await first.aclose()
                await second.aclose()
                backend.close()
        asyncio.run(verify())


if __name__ == "__main__":
    unittest.main()
