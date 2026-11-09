"""Offline quota admission checks with deterministic time and no credentials."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from importlib import import_module, util
import logging
import math
from threading import Barrier
import unittest
from unittest.mock import patch

from taip.ai_integration.errors import AIConfigurationError, AIProviderError


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.rate_limits"
        self.assertIsNotNone(util.find_spec(name), "The preventive quota limiter must exist")
        self.module = import_module(name)
        self.clock = FakeClock()
        self.events = []
        self.limiter = self.module.RateLimiter(clock=self.clock, observer=self.events.append)

    def policy(self, **changes):
        values = dict(requests_per_minute=10, requests_per_day=100,
                      tokens_per_minute=1000, max_concurrent=10)
        values.update(changes)
        return self.module.RateLimitPolicy(**values)

    def acquire(self, key="credential-fingerprint", *, policy=None, tokens=1, model_id="logical-a"):
        return self.limiter.acquire(
            key, policy=policy or self.policy(), estimated_tokens=tokens,
            provider="groq", model_id=model_id,
        )

    def denied(self, *, code="local_rate_limit", **options):
        with self.assertRaises(AIProviderError) as caught:
            self.acquire(**options)
        self.assertEqual(caught.exception.code, code)
        if code == "local_rate_limit":
            self.assertTrue(math.isfinite(caught.exception.retry_after_seconds))
            self.assertGreater(caught.exception.retry_after_seconds, 0)
        return caught.exception

    def test_policy_rejects_nonpositive_nonexact_integers_and_invalid_warning_fraction(self):
        for field in ("requests_per_minute", "requests_per_day", "tokens_per_minute", "max_concurrent"):
            for value in (0, -1, True, False, 1.5, "6", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(AIConfigurationError) as caught:
                        self.policy(**{field: value})
                    self.assertEqual(caught.exception.field, field)
        for value in (0, -0.1, 1.01, float("nan"), float("inf"), True, "0.8", None):
            with self.subTest(fraction=value):
                with self.assertRaises(AIConfigurationError) as caught:
                    self.policy(warning_fraction=value)
                self.assertEqual(caught.exception.field, "warning_fraction")
        self.policy(warning_fraction=1)

    def test_policy_event_and_snapshot_are_immutable(self):
        policy = self.policy()
        with self.assertRaises(FrozenInstanceError):
            policy.requests_per_minute = 500
        snapshot = self.limiter.snapshot("never-seen")
        self.assertEqual((snapshot.requests_in_minute, snapshot.requests_in_day,
                          snapshot.tokens_in_minute, snapshot.in_flight, snapshot.cooldown_seconds),
                         (0, 0, 0, 0, 0.0))
        with self.assertRaises(FrozenInstanceError):
            snapshot.in_flight = 1
        self.acquire(policy=self.policy(requests_per_minute=1))
        with self.assertRaises(FrozenInstanceError):
            self.events[0].used = 7

    def test_minute_admission_has_exact_rolling_boundary_and_denial_consumes_nothing(self):
        policy = self.policy(requests_per_minute=2)
        first = self.acquire(policy=policy, tokens=30)
        first.finish()
        self.clock.advance(20)
        self.acquire(policy=policy, tokens=40).finish()
        before = self.limiter.snapshot("credential-fingerprint")
        error = self.denied(policy=policy, tokens=50)
        self.assertEqual(error.retry_after_seconds, 40.0)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint"), before)
        self.clock.advance(39.999)
        self.denied(policy=policy)
        self.clock.advance(0.001)
        self.acquire(policy=policy, tokens=50).finish()
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.requests_in_minute, snapshot.requests_in_day,
                          snapshot.tokens_in_minute), (2, 3, 90))

    def test_day_budget_is_rolling_and_failed_attempts_still_count(self):
        policy = self.policy(requests_per_day=2)
        self.acquire(policy=policy).finish()
        self.clock.advance(60)
        self.acquire(policy=policy).finish()
        self.clock.advance(60)
        self.assertEqual(self.denied(policy=policy).retry_after_seconds, 86280.0)
        self.clock.advance(86280)
        self.acquire(policy=policy).finish()
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.requests_in_minute, snapshot.requests_in_day), (1, 2))

    def test_tokens_are_reserved_before_finish_and_actual_usage_releases_unused_budget(self):
        policy = self.policy(tokens_per_minute=100)
        permit = self.acquire(policy=policy, tokens=70)
        self.denied(policy=policy, tokens=31)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").tokens_in_minute, 70)
        permit.finish(actual_tokens=20)
        self.acquire(policy=policy, tokens=80).finish(actual_tokens=60)
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.tokens_in_minute, snapshot.in_flight), (80, 0))

    def test_zero_actual_usage_is_reported_usage_and_none_retains_reservation(self):
        self.acquire(tokens=40).finish(actual_tokens=0)
        self.acquire(tokens=60).finish()
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.tokens_in_minute, snapshot.requests_in_minute), (60, 2))

    def test_invalid_usage_is_ignored_and_actual_usage_can_exceed_reservation(self):
        for value in (-1, True, 1.0, "2", float("nan")):
            self.acquire(tokens=10).finish(actual_tokens=value)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").tokens_in_minute, 50)
        self.acquire(tokens=10).finish(actual_tokens=1100)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").tokens_in_minute, 1150)
        self.denied()

    def test_invalid_estimate_is_rejected_without_consuming_any_budget(self):
        for value in (-1, True, 1.0, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(AIConfigurationError) as caught:
                    self.acquire(tokens=value)
                self.assertEqual(caught.exception.field, "estimated_tokens")
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").requests_in_day, 0)
        self.acquire(tokens=0).finish(actual_tokens=0)

    def test_oversized_payload_is_permanent_and_does_not_pin_an_unused_key_policy(self):
        self.denied(code="request_budget_exceeded", policy=self.policy(tokens_per_minute=10), tokens=11)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").requests_in_day, 0)
        self.acquire(policy=self.policy(tokens_per_minute=20), tokens=11).finish()
        error = self.denied(code="request_budget_exceeded", policy=self.policy(tokens_per_minute=20), tokens=21)
        self.assertIsNone(error.retry_after_seconds)
        self.assertFalse(error.retryable)

    def test_token_retry_hint_waits_until_enough_settled_tokens_expire(self):
        policy = self.policy(tokens_per_minute=100)
        self.acquire(policy=policy, tokens=20).finish()
        self.clock.advance(20)
        self.acquire(policy=policy, tokens=60).finish()
        self.clock.advance(10)
        self.assertEqual(self.denied(policy=policy, tokens=50).retry_after_seconds, 50.0)
        self.clock.advance(50)
        self.acquire(policy=policy, tokens=100).finish()

    def test_out_of_order_finishes_expire_charges_from_admission_time(self):
        first = self.acquire(tokens=20)
        self.clock.advance(20)
        second = self.acquire(tokens=30)
        second.finish(actual_tokens=25)
        first.finish(actual_tokens=15)
        self.clock.advance(40)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").tokens_in_minute, 25)
        self.clock.advance(20)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").tokens_in_minute, 0)

    def test_inflight_reservations_do_not_expire_during_long_provider_call(self):
        policy = self.policy(tokens_per_minute=100, max_concurrent=3)
        permit = self.acquire(policy=policy, tokens=70)
        self.clock.advance(120)
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.requests_in_minute, snapshot.tokens_in_minute, snapshot.in_flight),
                         (0, 70, 1))
        self.denied(policy=policy, tokens=31)
        permit.finish(actual_tokens=50)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").tokens_in_minute, 0)
        self.acquire(policy=policy, tokens=100).finish()

    def test_concurrency_capacity_survives_window_expiry_until_finish(self):
        policy = self.policy(max_concurrent=1)
        permit = self.acquire(policy=policy)
        self.clock.advance(86401)
        self.assertEqual(self.denied(policy=policy).retry_after_seconds, 1.0)
        permit.finish()
        self.acquire(policy=policy).finish()

    def test_finish_is_idempotent_under_parallel_calls(self):
        permit = self.acquire(tokens=50)
        barrier = Barrier(12)

        def finish_once(_):
            barrier.wait()
            permit.finish(actual_tokens=20)

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(finish_once, range(12)))
        permit.finish(actual_tokens=999, cooldown_seconds=999)
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.tokens_in_minute, snapshot.in_flight, snapshot.cooldown_seconds),
                         (20, 0, 0.0))

    def test_parallel_admission_is_atomic_for_all_budgets(self):
        for changes, tokens, expected in (
            ({"requests_per_minute": 3}, 1, 3),
            ({"requests_per_day": 4}, 1, 4),
            ({"tokens_per_minute": 10}, 3, 3),
            ({"max_concurrent": 2}, 1, 2),
        ):
            with self.subTest(changes=changes):
                limiter = self.module.RateLimiter(clock=self.clock)
                policy = self.policy(**changes)
                barrier = Barrier(16)

                def race(_):
                    barrier.wait()
                    try:
                        return limiter.acquire("shared", policy=policy, estimated_tokens=tokens,
                                               provider="groq", model_id="logical")
                    except AIProviderError:
                        return None

                with ThreadPoolExecutor(max_workers=16) as executor:
                    permits = list(executor.map(race, range(16)))
                admitted = [permit for permit in permits if permit is not None]
                self.assertEqual(len(admitted), expected)
                snapshot = limiter.snapshot("shared")
                self.assertEqual((snapshot.requests_in_day, snapshot.in_flight, snapshot.tokens_in_minute),
                                 (expected, expected, expected * tokens))
                for permit in admitted:
                    permit.finish()

    def test_same_key_shares_limits_across_logical_model_aliases(self):
        policy = self.policy(requests_per_minute=1)
        self.acquire(policy=policy, model_id="logical-a").finish()
        self.denied(policy=policy, model_id="logical-b")
        self.acquire(key="other-credential", policy=policy, model_id="logical-a").finish()

    def test_same_key_rejects_policy_changes_before_pruning_or_other_mutation(self):
        self.acquire(tokens=50).finish()
        self.clock.advance(10)
        before = self.limiter.snapshot("credential-fingerprint")
        with self.assertRaises(AIConfigurationError) as caught:
            self.acquire(policy=self.policy(tokens_per_minute=900))
        self.assertEqual(caught.exception.field, "rate_limits")
        self.assertEqual(self.limiter.snapshot("credential-fingerprint"), before)
        self.clock.advance(86401)
        with self.assertRaises(AIConfigurationError):
            self.acquire(policy=self.policy(tokens_per_minute=900))

    def test_cooldown_blocks_admission_until_exact_boundary_and_never_shortens(self):
        first = self.acquire(tokens=20)
        second = self.acquire(tokens=30)
        first.finish(cooldown_seconds=60)
        self.clock.advance(10)
        second.finish(cooldown_seconds=5)
        before = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual(before.cooldown_seconds, 50.0)
        self.assertEqual(self.denied().retry_after_seconds, 50.0)
        self.assertEqual(self.limiter.snapshot("credential-fingerprint"), before)
        self.clock.advance(50)
        self.acquire().finish()
        self.assertTrue(any(event.kind == "cooldown" for event in self.events))

    def test_later_longer_cooldown_extends_existing_deadline(self):
        first = self.acquire()
        second = self.acquire()
        first.finish(cooldown_seconds=20)
        self.clock.advance(10)
        second.finish(cooldown_seconds=60)
        self.assertEqual(self.denied().retry_after_seconds, 60.0)

    def test_invalid_cooldown_hint_is_ignored_without_leaking_capacity(self):
        for value in (-1, True, "60", float("nan"), float("inf")):
            self.acquire().finish(cooldown_seconds=value)
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.in_flight, snapshot.cooldown_seconds), (0, 0.0))

    def test_near_limit_warning_is_once_per_crossing_and_rearms_after_expiry(self):
        policy = self.policy(requests_per_minute=5, max_concurrent=100)
        for _ in range(5):
            self.acquire(policy=policy).finish()
        near = [event for event in self.events
                if event.kind == "near_limit" and event.dimension == "requests_per_minute"]
        self.assertEqual([(event.used, event.limit) for event in near], [(4, 5)])
        self.clock.advance(60)
        for _ in range(4):
            self.acquire(policy=policy).finish()
        near = [event for event in self.events
                if event.kind == "near_limit" and event.dimension == "requests_per_minute"]
        self.assertEqual(len(near), 2)

    def test_token_warning_rearms_when_actual_usage_drops_below_threshold(self):
        policy = self.policy(tokens_per_minute=100)
        self.acquire(policy=policy, tokens=80).finish(actual_tokens=10)
        self.acquire(policy=policy, tokens=70).finish(actual_tokens=70)
        near = [event for event in self.events
                if event.kind == "near_limit" and event.dimension == "tokens_per_minute"]
        self.assertEqual([(event.used, event.limit) for event in near], [(80, 100), (80, 100)])

    def test_observer_runs_outside_lock_and_can_reenter_from_another_thread(self):
        snapshots = []

        def observer(_event):
            with ThreadPoolExecutor(max_workers=1) as executor:
                snapshots.append(executor.submit(self.limiter.snapshot, "credential-fingerprint").result(timeout=2))

        self.limiter = self.module.RateLimiter(clock=self.clock, observer=observer)
        permit = self.acquire(policy=self.policy(requests_per_minute=1))
        self.assertTrue(snapshots)
        self.assertEqual(snapshots[0].in_flight, 1)
        permit.finish()

    def test_reentrant_observer_does_not_recursively_emit_denials(self):
        observed = []
        policy = self.policy(max_concurrent=1)

        def observer(event):
            observed.append(event)
            self.denied(policy=policy)

        self.limiter = self.module.RateLimiter(clock=self.clock, observer=observer)
        permit = self.acquire(policy=policy)
        self.assertEqual(len(observed), 1)
        permit.finish()
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").in_flight, 0)

    def test_observer_and_logging_failures_do_not_change_admission_or_release(self):
        def broken_observer(_event):
            raise RuntimeError("private-observer-content")

        self.limiter = self.module.RateLimiter(clock=self.clock, observer=broken_observer)
        with patch.object(logging.Logger, "warning", side_effect=RuntimeError("private-handler-content")):
            permit = self.acquire(policy=self.policy(requests_per_minute=1))
            self.denied(policy=self.policy(requests_per_minute=1))
            permit.finish(actual_tokens=0, cooldown_seconds=5)
        snapshot = self.limiter.snapshot("credential-fingerprint")
        self.assertEqual((snapshot.requests_in_day, snapshot.tokens_in_minute, snapshot.in_flight,
                          snapshot.cooldown_seconds), (1, 0, 0, 5.0))

    def test_logs_have_safe_structured_fields_and_never_format_opaque_key(self):
        class SecretKey:
            def __repr__(self):
                raise AssertionError("Credential keys must never be rendered")

        key = SecretKey()
        policy = self.policy(requests_per_minute=1)
        with self.assertLogs("taip.ai_integration.rate_limits", level="WARNING") as captured:
            permit = self.acquire(key=key, policy=policy)
            self.denied(key=key, policy=policy)
            permit.finish(cooldown_seconds=5)
        self.assertGreaterEqual(len(captured.records), 3)
        for record in captured.records:
            self.assertEqual(getattr(record, "quota_event", None), "ai.quota")
            message = record.getMessage()
            self.assertIn(record.quota_kind, message)
            self.assertIn("groq", message)
            self.assertIn("logical-a", message)
            self.assertIn(record.quota_dimension, message)
            self.assertIn(f"{record.quota_used}/{record.quota_limit}", message)
            self.assertEqual(record.quota_provider, "groq")
            self.assertEqual(record.quota_model_id, "logical-a")
            self.assertIsInstance(record.quota_kind, str)
            self.assertIsInstance(record.quota_dimension, str)
            self.assertIsInstance(record.quota_used, int)
            self.assertIsInstance(record.quota_limit, int)
            self.assertIn("quota_retry_after_seconds", vars(record))
            self.assertNotIn("key", vars(record))
            self.assertIsNone(record.exc_info)

    def test_console_warning_displays_budget_numbers_and_retry_delay(self):
        policy = self.policy(tokens_per_minute=100)
        self.acquire(policy=policy, tokens=70).finish()
        with self.assertLogs("taip.ai_integration.rate_limits", level="WARNING") as captured:
            error = self.denied(policy=policy, tokens=40)
        self.assertEqual(error.retry_after_seconds, 60.0)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertIn("blocked", message)
        self.assertIn("tokens_per_minute", message)
        self.assertIn("70/100", message)
        self.assertIn("60", message)

    def test_repeated_denials_warn_once_and_still_notify_observer_for_every_attempt(self):
        policy = self.policy(requests_per_minute=1)
        self.acquire(policy=policy).finish()
        self.events.clear()
        with self.assertLogs("taip.ai_integration.rate_limits", level="WARNING") as captured:
            for _ in range(3):
                self.denied(policy=policy)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(len(self.events), 3)
        self.assertTrue(all(event.kind == "blocked" for event in self.events))
        self.assertEqual(self.limiter.snapshot("credential-fingerprint").requests_in_day, 1)
        self.clock.advance(60)
        self.acquire(policy=policy).finish()
        with self.assertLogs("taip.ai_integration.rate_limits", level="WARNING") as captured:
            self.denied(policy=policy)
        self.assertEqual(len(captured.records), 1)

    def test_warning_dedup_is_per_dimension_and_per_admitted_key(self):
        policy = self.policy(requests_per_minute=1, tokens_per_minute=100, max_concurrent=1)
        first = self.acquire(policy=policy)
        second = self.acquire(key="other-credential", policy=policy)
        with self.assertLogs("taip.ai_integration.rate_limits", level="WARNING") as captured:
            self.denied(policy=policy)
            self.denied(policy=policy)
            self.denied(key="other-credential", policy=policy)
            first.finish()
            self.denied(policy=policy)
            self.denied(policy=policy)
        blocked = [record for record in captured.records if record.quota_kind == "blocked"]
        self.assertEqual([record.quota_dimension for record in blocked],
                         ["max_concurrent", "max_concurrent", "requests_per_minute"])
        second.finish()

    def test_unknown_permanent_denials_do_not_allocate_dedup_or_policy_state(self):
        policy = self.policy(tokens_per_minute=10)
        with self.assertLogs("taip.ai_integration.rate_limits", level="WARNING") as captured:
            for _ in range(3):
                self.denied(code="request_budget_exceeded", policy=policy, tokens=11)
        self.assertEqual(len(captured.records), 3)
        self.acquire(policy=self.policy(tokens_per_minute=20), tokens=11).finish()


if __name__ == "__main__":
    unittest.main()
