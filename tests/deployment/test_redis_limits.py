"""Offline Lua contract checks; these do not prove cross-process Redis behavior."""

from concurrent.futures import ThreadPoolExecutor
from importlib import import_module, util
import logging
import json
import unittest
from unittest.mock import patch

import fakeredis

from taip.ai_integration.errors import AIConfigurationError, AIProviderError
from taip.ai_integration.rate_limits import RateLimitPolicy, QuotaSnapshot


class RedisRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(util.find_spec("taip.deployment.redis_limits"),
                             "The shared Redis admission backend must exist")
        self.module = import_module("taip.deployment.redis_limits")
        self.now = 1_000_000.0
        self.timer = patch("fakeredis.commands_mixins.server_mixin.time.time",
                           side_effect=lambda: self.now)
        self.timer.start()
        self.addCleanup(self.timer.stop)
        self.server = fakeredis.FakeServer()
        self.client = fakeredis.FakeRedis(server=self.server)
        self.addCleanup(self.client.close)
        self.events = []
        self.limiter = self.module.RedisRateLimiter(self.client, observer=self.events.append)
        self.key = ("groq", b"private-credential-digest")

    def policy(self, **changes):
        values = dict(requests_per_minute=10, requests_per_day=100,
                      tokens_per_minute=1000, max_concurrent=10)
        values.update(changes)
        return RateLimitPolicy(**values)

    def acquire(self, *, limiter=None, policy=None, tokens=10, **options):
        return (limiter or self.limiter).acquire(
            self.key, policy=policy or self.policy(), estimated_tokens=tokens,
            provider="groq", model_id="logical-a", **options,
        )

    def denied(self, code="local_rate_limit", **options):
        with self.assertRaises(AIProviderError) as caught:
            self.acquire(**options)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_shared_instances_reserve_and_reconcile_actual_zero_exactly_once(self):
        second = self.module.RedisRateLimiter(fakeredis.FakeRedis(server=self.server))
        first = self.acquire(tokens=80)
        self.assertEqual(second.snapshot(self.key), QuotaSnapshot(1, 1, 80, 1, 0.0))
        first.finish(actual_tokens=0)
        first.finish(actual_tokens=900)
        self.assertEqual(second.snapshot(self.key), QuotaSnapshot(1, 1, 0, 0, 0.0))

    def test_rolling_requests_expire_and_denials_do_not_charge(self):
        policy = self.policy(requests_per_minute=2, requests_per_day=3)
        self.acquire(policy=policy, tokens=30).finish()
        self.now += 20
        self.acquire(policy=policy, tokens=40).finish()
        before = self.limiter.snapshot(self.key)
        self.assertEqual(self.denied(policy=policy).retry_after_seconds, 40.0)
        self.assertEqual(self.limiter.snapshot(self.key), before)
        self.now += 40
        self.acquire(policy=policy, tokens=50).finish()
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(2, 3, 90, 0, 0.0))
        self.now += 60
        self.assertEqual(self.denied(policy=policy).retry_after_seconds, 86280.0)
        self.now = 1_086_400.0
        self.acquire(policy=policy).finish()
        self.assertEqual(self.limiter.snapshot(self.key).requests_in_day, 3)

    def test_active_tokens_survive_minute_but_finished_tokens_expire_at_admission(self):
        first = self.acquire(tokens=80)
        self.now += 61
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(0, 1, 80, 1, 0.0))
        first.finish(actual_tokens=90)
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(0, 1, 0, 0, 0.0))

    def test_token_budget_and_unknown_usage_are_conservative(self):
        policy = self.policy(tokens_per_minute=100)
        first = self.acquire(policy=policy, tokens=80)
        self.denied(policy=policy, tokens=21)
        first.finish(actual_tokens=True)
        self.assertEqual(self.limiter.snapshot(self.key).tokens_in_minute, 80)
        self.assertEqual(self.denied(policy=policy, tokens=21).retry_after_seconds, 60.0)
        self.denied("request_budget_exceeded", policy=policy, tokens=101)
        self.assertEqual(self.limiter.snapshot(self.key).requests_in_day, 1)

    def test_shared_cooldown_keeps_request_charges(self):
        self.acquire().finish(cooldown_seconds=30)
        self.assertEqual(self.denied().retry_after_seconds, 30.0)
        self.now += 30
        self.acquire().finish(cooldown_seconds=float("nan"))
        self.assertEqual(self.limiter.snapshot(self.key).requests_in_day, 2)

    def test_idempotent_acquisition_replay_does_not_double_charge(self):
        first = self.acquire(acquisition_id="operation-a", tokens=80)
        second = self.acquire(acquisition_id="operation-a", tokens=80)
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(1, 1, 80, 1, 0.0))
        second.finish(actual_tokens=90)
        first.finish(actual_tokens=0)
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(1, 1, 90, 0, 0.0))

    def test_lease_expiry_recovers_capacity_and_lost_finish_fails_closed(self):
        limiter = self.module.RedisRateLimiter(self.client, lease_seconds=100)
        policy = self.policy(max_concurrent=1)
        first = self.acquire(limiter=limiter, policy=policy, tokens=80)
        self.denied(limiter=limiter, policy=policy)
        self.now += 100
        self.assertEqual(limiter.snapshot(self.key), QuotaSnapshot(0, 1, 0, 0, 0.0))
        self.acquire(limiter=limiter, policy=policy)
        with self.assertRaises(AIProviderError) as caught:
            first.finish(actual_tokens=0)
        self.assertEqual(caught.exception.code, "admission_unavailable")

    def test_policy_binds_only_after_admission_and_conflicts_across_instances(self):
        policy = self.policy(tokens_per_minute=100)
        self.denied("request_budget_exceeded", policy=policy, tokens=101)
        self.acquire(policy=self.policy())
        with self.assertRaises(AIConfigurationError) as caught:
            self.acquire(policy=policy)
        self.assertEqual(caught.exception.field, "rate_limits")

    def test_concurrent_instances_cannot_admit_more_than_shared_capacity(self):
        policy = self.policy(max_concurrent=3)
        def attempt(_):
            limiter = self.module.RedisRateLimiter(fakeredis.FakeRedis(server=self.server))
            try:
                return self.acquire(limiter=limiter, policy=policy)
            except AIProviderError as error:
                self.assertEqual(error.code, "local_rate_limit")
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(20)))
        self.assertEqual(sum(value is not None for value in results), 3)
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(3, 3, 30, 3, 0.0))

    def test_opaque_keys_are_unambiguous_and_share_one_cluster_slot(self):
        self.acquire().finish()
        keys = self.client.keys()
        self.assertTrue(keys)
        self.assertTrue(all(b"private-credential-digest" not in key and b"groq" not in key for key in keys))
        from redis.cluster import key_slot
        self.assertEqual(len({key_slot(key) for key in keys}), 1)
        self.assertEqual(self.limiter.snapshot((b"groq", "private-credential-digest")),
                         QuotaSnapshot(0, 0, 0, 0, 0.0))

    def test_safe_backend_errors_and_missing_permit_fail_closed(self):
        class BrokenClient:
            def eval(self, *args):
                raise RuntimeError("redis://user:password@private.example/credential")
        unavailable = self.module.RedisRateLimiter(BrokenClient())
        error = self.denied("admission_unavailable", limiter=unavailable)
        self.assertEqual(error.retry_after_seconds, 1)
        self.assertNotIn("password", str(error))
        self.assertIsNone(error.__cause__)
        first = self.acquire()
        self.client.flushdb()
        with self.assertRaises(AIProviderError) as caught:
            first.finish()
        self.assertEqual(caught.exception.code, "admission_unavailable")

    def test_protocol_failure_never_becomes_an_admission(self):
        class InvalidClient:
            def eval(self, *args):
                return b'{"status":"ok"}'
        limiter = self.module.RedisRateLimiter(InvalidClient())
        self.denied("admission_unavailable", limiter=limiter)

    def test_protocol_missing_required_fields_and_unknown_status_fail_closed(self):
        complete = {"version": 1, "status": "admitted", "code": None,
                    "retry_after_seconds": None,
                    "snapshot": {"requests_in_minute": 1, "requests_in_day": 1,
                                 "tokens_in_minute": 10, "in_flight": 1, "cooldown_seconds": 0},
                    "events": []}
        responses = []
        for field in complete:
            modified = dict(complete)
            del modified[field]
            responses.append(modified)
        responses.extend(({**complete, "status": "unexpected"},
                          {**complete, "version": True},
                          {**complete, "code": "unexpected"}))
        for response in responses:
            with self.subTest(response=response):
                class InvalidClient:
                    def eval(self, *args):
                        return json.dumps(response)
                limiter = self.module.RedisRateLimiter(InvalidClient())
                self.denied("admission_unavailable", limiter=limiter)

    def test_ambiguous_acquire_response_replays_same_operation_without_double_debit(self):
        real_client = self.client
        class AmbiguousClient:
            failed = False
            def eval(self, *args):
                result = real_client.eval(*args)
                if not self.failed:
                    self.failed = True
                    raise TimeoutError("connection credential hidden")
                return result
        limiter = self.module.RedisRateLimiter(AmbiguousClient())
        self.denied("admission_unavailable", limiter=limiter, acquisition_id="ambiguous-a", tokens=80)
        permit = self.acquire(limiter=limiter, acquisition_id="ambiguous-a", tokens=80)
        self.assertEqual(limiter.snapshot(self.key), QuotaSnapshot(1, 1, 80, 1, 0.0))
        permit.finish()

    def test_short_expired_lease_retains_token_estimate_until_minute_boundary(self):
        limiter = self.module.RedisRateLimiter(self.client, lease_seconds=10)
        policy = self.policy(tokens_per_minute=100)
        self.acquire(limiter=limiter, policy=policy, tokens=80)
        self.now += 10
        self.assertEqual(limiter.snapshot(self.key), QuotaSnapshot(1, 1, 80, 0, 0.0))
        self.denied(limiter=limiter, policy=policy, tokens=21)
        self.now += 50
        self.acquire(limiter=limiter, policy=policy, tokens=80)

    def test_observer_failure_and_reentry_cannot_change_atomic_admission(self):
        calls = []
        policy = self.policy(requests_per_minute=2, warning_fraction=0.5)
        def observer(event):
            calls.append(event)
            self.acquire(limiter=limiter, policy=policy)
            raise RuntimeError("observer failed")
        limiter = self.module.RedisRateLimiter(self.client, observer=observer)
        self.acquire(limiter=limiter, policy=policy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(limiter.snapshot(self.key), QuotaSnapshot(2, 2, 20, 2, 0.0))

    def test_bucket_has_bounded_ttl_and_cooldown_cannot_expire_early(self):
        self.acquire().finish(cooldown_seconds=90000)
        self.assertTrue(all(0 < self.client.pttl(key) <= 176400000 for key in self.client.keys()))
        self.now += 87400
        self.assertEqual(self.denied().retry_after_seconds, 2600.0)
        self.now += 2600
        self.acquire()

    def test_bounded_cleanup_fails_closed_until_expired_batch_is_drained(self):
        policy = self.policy(requests_per_minute=200, requests_per_day=500)
        for _ in range(150):
            self.acquire(policy=policy, tokens=1).finish()
        self.now += 60
        with self.assertRaises(AIProviderError) as caught:
            self.limiter.snapshot(self.key)
        self.assertEqual(caught.exception.code, "admission_unavailable")
        self.assertEqual(self.limiter.snapshot(self.key), QuotaSnapshot(0, 150, 0, 0, 0.0))

    def test_large_safe_integer_budgets_never_round_away_reserved_tokens(self):
        policy = self.policy(tokens_per_minute=9007199254740991)
        permit = self.acquire(policy=policy, tokens=123456789012345)
        self.assertEqual(self.limiter.snapshot(self.key).tokens_in_minute, 123456789012345)
        permit.finish(actual_tokens=123456789012344)
        self.assertEqual(self.limiter.snapshot(self.key).tokens_in_minute, 123456789012344)
        self.now += 60
        self.assertEqual(self.limiter.snapshot(self.key).tokens_in_minute, 0)

    def test_counter_overflow_quarantines_bucket_instead_of_releasing_rounded_capacity(self):
        policy = self.policy(tokens_per_minute=9007199254740991)
        first = self.acquire(policy=policy, tokens=1)
        second = self.acquire(policy=policy, tokens=1)
        with self.assertRaises(AIProviderError) as caught:
            first.finish(actual_tokens=9007199254740991)
        self.assertEqual(caught.exception.code, "admission_unavailable")
        self.now += 60
        with self.assertRaises(AIProviderError) as caught:
            second.finish(actual_tokens=0)
        self.assertEqual(caught.exception.code, "admission_unavailable")
        self.denied("admission_unavailable", policy=policy, tokens=0)

    def test_shared_warnings_and_safe_metadata_observer(self):
        policy = self.policy(requests_per_minute=2, warning_fraction=0.5)
        second_events = []
        second = self.module.RedisRateLimiter(self.client, observer=second_events.append)
        with self.assertLogs("taip.deployment.redis_limits", level=logging.WARNING) as captured:
            self.acquire(policy=policy).finish()
            self.acquire(limiter=second, policy=policy).finish()
            self.denied(policy=policy)
        events = self.events + second_events
        minute_warnings = [event for event in events if event.kind == "near_limit"
                           and event.dimension == "requests_per_minute"]
        self.assertEqual(len(minute_warnings), 1)
        self.assertTrue(all(record.quota_provider == "groq" for record in captured.records))
        self.assertTrue(all(record.quota_event == "ai.quota" for record in captured.records))
        self.assertNotIn("private-credential-digest", " ".join(captured.output))

    def test_invalid_namespace_keys_and_estimates_reject_locally(self):
        for namespace in ("", "a{slot}", "a\nsecret", "x" * 101):
            with self.subTest(namespace=namespace), self.assertRaises(AIConfigurationError):
                self.module.RedisRateLimiter(self.client, namespace=namespace)
        for key in (123, ["a"], ("a", 42), ("a", ("b",))):
            with self.subTest(key=key), self.assertRaises(AIConfigurationError):
                self.limiter.snapshot(key)
        for tokens in (-1, True, 1.5):
            with self.subTest(tokens=tokens), self.assertRaises(AIConfigurationError):
                self.acquire(tokens=tokens)


if __name__ == "__main__":
    unittest.main()
