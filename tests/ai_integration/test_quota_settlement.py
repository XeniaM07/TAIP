"""Paid results survive ambiguous quota settlement without retrying the vendor."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4
import unittest
from unittest.mock import patch

import fakeredis
from redis.exceptions import ConnectionError

from taip.ai_integration.config import ProviderConfig, ProviderKind, provider_quota_key
from taip.ai_integration.errors import AIProviderError, AIResponseError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.rate_limits import RateLimitPolicy
from taip.ai_integration.settlement import SettlementCoordinator
from taip.common.timetable_contracts import OriginalProblem, TimetableData, TimetableProblemSnapshot
from taip.deployment.redis_limits import RedisRateLimiter
from tests.ai_integration.test_providers import RecordingTransport, completion


class AmbiguousFinishClient:
    """Execute real Redis Lua once, then lose its first finish acknowledgement."""

    def __init__(self, client):
        self.client = client
        self.lose_next_finish_reply = True
        self.reject_finish = False
        self.finish_attempts = 0

    def eval(self, script, count, *arguments):
        operation = arguments[count]
        if operation == "finish":
            self.finish_attempts += 1
            if self.reject_finish:
                raise ConnectionError("redis://private-user:private-password@private-host")
        result = self.client.eval(script, count, *arguments)
        if operation == "finish" and self.lose_next_finish_reply:
            self.lose_next_finish_reply = False
            self.reject_finish = True
            raise ConnectionError("redis://private-user:private-password@private-host")
        return result


class QuotaSettlementTests(unittest.TestCase):
    def setUp(self):
        self.client = fakeredis.FakeRedis(server=fakeredis.FakeServer())
        self.addCleanup(self.client.close)
        self.backend = AmbiguousFinishClient(self.client)
        self.limiter = RedisRateLimiter(self.backend)
        self.policy = RateLimitPolicy(requests_per_minute=50, requests_per_day=100,
                                      tokens_per_minute=1_000_000, max_concurrent=4)

    def config(self, kind=ProviderKind.GROQ):
        return ProviderConfig(kind.value, kind, "vendor-model", f"fake-key-{uuid4().hex}",
                              rate_limits=self.policy)

    def invoke(self, provider, operation):
        if operation == "extract":
            return provider.extract(OriginalProblem("problem", "private source"))
        return provider.solve(TimetableProblemSnapshot(
            "snapshot", "draft", "problem", provider.model_id, 1, "private source", TimetableData(),
        ))

    def test_applied_finish_with_lost_reply_preserves_paid_extract_and_solve(self):
        for kind in ProviderKind:
            for operation, raw in (("extract", '{"data":{}}'), ("solve", '{"assignments":[]}')):
                with self.subTest(provider=kind, operation=operation):
                    config = self.config(kind)
                    envelope = completion(kind, raw)
                    if kind is ProviderKind.GEMINI:
                        envelope["usageMetadata"] = {"totalTokenCount": 7}
                    else:
                        envelope["usage"] = {"total_tokens": 7}
                    transport = RecordingTransport(envelope, completion(kind, raw))
                    records = []
                    provider = AIProviderFactory((config,), transport=transport,
                                                 observer=records.append, limiter=self.limiter).get_provider(kind.value)
                    self.backend.lose_next_finish_reply = True
                    self.backend.reject_finish = False

                    result = self.invoke(provider, operation)

                    if operation == "extract":
                        self.assertEqual(result.raw_response, raw)
                    else:
                        self.assertEqual(result.assignments, ())
                        self.assertEqual(result.snapshot_id, "snapshot")
                    self.assertEqual(len(transport.requests), 1)
                    self.assertIs(records[0].result, result)
                    self.assertEqual(records[0].outcome, "success")
                    self.assertIsNone(records[0].error_code)
                    self.assertEqual(records[0].raw_response, raw)
                    self.assertTrue(records[0].settlement_pending)
                    self.assertEqual(records[0].settlement_warning, "quota_settlement_pending")
                    snapshot = self.limiter.snapshot(provider_quota_key(config))
                    self.assertEqual((snapshot.requests_in_day, snapshot.tokens_in_minute, snapshot.in_flight),
                                     (1, 7, 0))

                    with self.assertRaises(AIProviderError) as caught:
                        self.invoke(provider, operation)
                    self.assertEqual(caught.exception.code, "admission_unavailable")
                    self.assertEqual(len(transport.requests), 1)
                    self.assertTrue(records[-1].settlement_pending)

                    self.backend.reject_finish = False
                    self.invoke(provider, operation)
                    self.assertEqual(len(transport.requests), 2)
                    self.assertFalse(records[-1].settlement_pending)
                    self.assertEqual(self.limiter.snapshot(provider_quota_key(config)).requests_in_day, 2)

    def test_another_factory_and_alias_cannot_bypass_pending_same_credential(self):
        config = self.config()
        first_transport = RecordingTransport(completion(ProviderKind.GROQ))
        first = AIProviderFactory((config,), transport=first_transport,
                                  limiter=self.limiter).get_provider(config.model_id)
        self.invoke(first, "extract")
        alias = replace(config, model_id="another-alias")
        second_transport = RecordingTransport(completion(ProviderKind.GROQ))
        second = AIProviderFactory((alias,), transport=second_transport,
                                   limiter=RedisRateLimiter(self.client)).get_provider(alias.model_id)

        with self.assertRaises(AIProviderError) as caught:
            self.invoke(second, "extract")

        self.assertEqual(caught.exception.code, "admission_unavailable")
        self.assertEqual(second_transport.requests, [])
        self.assertEqual(len(first_transport.requests), 1)
        self.backend.reject_finish = False
        self.invoke(second, "extract")
        self.assertEqual(self.limiter.snapshot(provider_quota_key(config)).requests_in_day, 2)

    def test_original_429_and_cooldown_survive_failed_settlement(self):
        config = self.config()
        failure = AIProviderError("rate_limit", status_code=429, retry_after_seconds=30)
        transport = RecordingTransport(failure)
        records = []
        provider = AIProviderFactory((config,), transport=transport, observer=records.append,
                                     limiter=self.limiter).get_provider(config.model_id)

        with self.assertRaises(AIProviderError) as caught:
            self.invoke(provider, "extract")

        self.assertIs(caught.exception, failure)
        self.assertEqual((records[0].error_code, records[0].http_status_code), ("rate_limit", 429))
        self.assertTrue(records[0].settlement_pending)
        self.assertGreater(self.limiter.snapshot(provider_quota_key(config)).cooldown_seconds, 25)
        self.backend.reject_finish = False
        with self.assertRaises(AIProviderError) as recovered:
            self.invoke(provider, "extract")
        self.assertEqual(recovered.exception.code, "local_rate_limit")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.limiter.snapshot(provider_quota_key(config)).requests_in_day, 1)

    def test_paid_invalid_json_retains_original_error_and_private_raw_response(self):
        config = self.config()
        raw = "private malformed model text"
        transport = RecordingTransport(completion(ProviderKind.GROQ, raw), completion(ProviderKind.GROQ))
        records = []
        provider = AIProviderFactory((config,), transport=transport, observer=records.append,
                                     limiter=self.limiter).get_provider(config.model_id)

        with self.assertRaises(AIResponseError) as caught:
            self.invoke(provider, "extract")

        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertEqual(records[0].raw_response, raw)
        self.assertEqual(records[0].error_code, "invalid_json")
        self.assertTrue(records[0].settlement_pending)
        for secret in (raw, config.api_key, "private-password", "private-host"):
            self.assertNotIn(secret, repr(records[0]))
            self.assertNotIn(secret, str(caught.exception))
        self.backend.reject_finish = False
        self.invoke(provider, "extract")

    def test_pending_warning_log_contains_safe_accounting_metadata_only(self):
        config = self.config()
        raw = '{"data":{}}'
        transport = RecordingTransport(completion(ProviderKind.GROQ, raw), completion(ProviderKind.GROQ))
        provider = AIProviderFactory((config,), transport=transport,
                                     limiter=self.limiter).get_provider(config.model_id)
        with self.assertLogs("taip.ai_integration.monitoring", level="WARNING") as captured:
            self.invoke(provider, "extract")
        record = captured.records[0]
        self.assertEqual(record.ai_outcome, "success")
        self.assertTrue(record.ai_settlement_pending)
        self.assertEqual(record.ai_settlement_warning, "quota_settlement_pending")
        self.assertIsNone(record.exc_info)
        for secret in (config.api_key, "private source", raw, "private-password", "private-host"):
            self.assertNotIn(secret, repr(vars(record)))
        self.backend.reject_finish = False
        self.invoke(provider, "extract")

    def test_bounded_recovery_capacity_rejects_before_vendor_and_reopens_after_settlement(self):
        coordinator = SettlementCoordinator(max_records=1)
        first_config, second_config = self.config(), self.config()
        first_transport = RecordingTransport(completion(ProviderKind.GROQ), completion(ProviderKind.GROQ))
        second_transport = RecordingTransport(completion(ProviderKind.GROQ))
        first = AIProviderFactory((first_config,), transport=first_transport,
                                  limiter=self.limiter).get_provider(first_config.model_id)
        second = AIProviderFactory((second_config,), transport=second_transport,
                                   limiter=self.limiter).get_provider(second_config.model_id)
        with patch("taip.ai_integration.providers.settlements", coordinator):
            self.invoke(first, "extract")
            for _ in range(5):
                with self.assertRaises(AIProviderError) as caught:
                    self.invoke(second, "extract")
                self.assertEqual(caught.exception.code, "admission_unavailable")
            self.assertEqual(second_transport.requests, [])
            self.assertEqual(self.limiter.snapshot(provider_quota_key(second_config)).requests_in_day, 0)
            self.backend.reject_finish = False
            self.invoke(first, "extract")
            self.invoke(second, "extract")
        self.assertEqual((len(first_transport.requests), len(second_transport.requests)), (2, 1))

    def test_failed_reconciliation_blocks_concurrent_aliases_without_vendor_calls(self):
        config = self.config()
        first_transport = RecordingTransport(completion(ProviderKind.GROQ), completion(ProviderKind.GROQ))
        first = AIProviderFactory((config,), transport=first_transport,
                                  limiter=self.limiter).get_provider(config.model_id)
        self.invoke(first, "extract")
        aliases = [replace(config, model_id=f"alias-{index}") for index in range(6)]
        transport = RecordingTransport()
        factory = AIProviderFactory(aliases, transport=transport,
                                    limiter=RedisRateLimiter(self.client))
        barrier = Barrier(6)

        def invoke_alias(alias):
            barrier.wait(timeout=3)
            try:
                self.invoke(factory.get_provider(alias.model_id), "extract")
            except AIProviderError as error:
                return error.code
            self.fail("An unresolved credential admitted a vendor call.")

        with ThreadPoolExecutor(max_workers=6) as executor:
            outcomes = list(executor.map(invoke_alias, aliases))
        self.assertEqual(outcomes, ["admission_unavailable"] * 6)
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.limiter.snapshot(provider_quota_key(config)).requests_in_day, 1)
        self.backend.reject_finish = False
        self.invoke(first, "extract")

    def test_unrelated_credential_progresses_while_another_settlement_waits_for_redis(self):
        entered, release = Event(), Event()
        real_client = self.client

        class WaitingClient:
            def eval(self, script, count, *arguments):
                if arguments[count] == "finish":
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("Test did not release the completion barrier.")
                return real_client.eval(script, count, *arguments)

        first_config, second_config = self.config(), self.config()
        first_transport = RecordingTransport(completion(ProviderKind.GROQ))
        second_transport = RecordingTransport(completion(ProviderKind.GROQ))
        first = AIProviderFactory((first_config,), transport=first_transport,
                                  limiter=RedisRateLimiter(WaitingClient())).get_provider(first_config.model_id)
        second = AIProviderFactory((second_config,), transport=second_transport,
                                   limiter=RedisRateLimiter(self.client)).get_provider(second_config.model_id)
        with ThreadPoolExecutor(max_workers=2) as executor:
            waiting = executor.submit(self.invoke, first, "extract")
            try:
                self.assertTrue(entered.wait(timeout=3))
                independent = executor.submit(self.invoke, second, "extract")
                self.assertEqual(independent.result(timeout=3).data, TimetableData())
            finally:
                release.set()
            self.assertEqual(waiting.result(timeout=3).data, TimetableData())
        self.assertEqual((len(first_transport.requests), len(second_transport.requests)), (1, 1))

    def test_expired_or_forgotten_permit_releases_pending_capacity_only_after_server_reconciliation(self):
        for applied_before_loss in (False, True):
            for elapsed in (2, 86402):
                with self.subTest(applied_before_loss=applied_before_loss, elapsed=elapsed):
                    now = [1_000_000.0]
                    config = self.config()
                    limiter = RedisRateLimiter(self.backend, lease_seconds=1)
                    coordinator = SettlementCoordinator(max_records=1)
                    key = provider_quota_key(config)
                    self.backend.lose_next_finish_reply = applied_before_loss
                    self.backend.reject_finish = not applied_before_loss
                    with patch("fakeredis.commands_mixins.server_mixin.time.time", side_effect=lambda: now[0]):
                        tracked = coordinator.acquire(limiter, key, policy=self.policy, estimated_tokens=80,
                                                      provider="groq", model_id="logical-a")
                        self.assertFalse(tracked.finish(actual_tokens=7))
                        now[0] += elapsed
                        self.backend.reject_finish = False
                        self.assertEqual(limiter.snapshot(key).in_flight, 0)
                        fresh = coordinator.acquire(limiter, key, policy=self.policy, estimated_tokens=10,
                                                    provider="groq", model_id="logical-a")
                        self.assertTrue(fresh.finish(actual_tokens=3))
                        snapshot = limiter.snapshot(key)
                    self.assertEqual(snapshot.requests_in_day, 2 if elapsed == 2 else 1)
                    self.assertEqual(snapshot.tokens_in_minute, 10 if elapsed == 2 else 3)
                    self.assertEqual(snapshot.in_flight, 0)


if __name__ == "__main__":
    unittest.main()
