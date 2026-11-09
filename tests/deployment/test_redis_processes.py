"""Opt-in real Redis contracts using independent spawned worker processes.

Set only TAIP_TEST_REDIS_URL to a disposable integration Redis database. The
tests never flush a database: each test deletes only its random namespace.
Absence of the opt-in skips this class; an explicitly unreachable server fails.
"""

from collections import Counter
from contextlib import contextmanager
import logging
import multiprocessing
import os
from queue import Empty
import time
import unittest
from uuid import uuid4

from taip.ai_integration.errors import AIProviderError
from taip.ai_integration.rate_limits import RateLimitPolicy
from taip.deployment.redis_limits import RedisRateLimiter


_KEY = ("integration", b"synthetic-test-credential-digest")


def _client(url):
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3,
                                retry=Retry(NoBackoff(), 0), retry_on_error=[])


def _quota_worker(url, namespace, worker, attempts, policy_values, shared_id,
                  cooldown, ready, start, release, admitted, finished):
    """Keep reservations active until the parent inspects their shared state."""
    client = None
    logging.getLogger("taip.deployment.redis_limits").disabled = True
    try:
        client = _client(url)
        limiter = RedisRateLimiter(client, namespace=namespace)
        policy = RateLimitPolicy(**policy_values)
        permits, failures = [], Counter()
        ready.put(worker)
        if not start.wait(20):
            raise TimeoutError
        for _ in range(attempts):
            try:
                permits.append(limiter.acquire(
                    _KEY, policy=policy, estimated_tokens=10, provider="integration",
                    model_id="logical-test", acquisition_id=shared_id,
                ))
            except AIProviderError as error:
                failures[error.code] += 1
        admitted.put({"worker": worker, "accepted": len(permits), "denied": dict(failures)})
        if not release.wait(20):
            raise TimeoutError
        for permit in permits:
            permit.finish(actual_tokens=3, cooldown_seconds=cooldown)
            # Competing processes may settle the same permit. A duplicate must
            # neither debit this deliberately different usage nor extend cooldown.
            permit.finish(actual_tokens=999, cooldown_seconds=999)
        finished.put({"worker": worker, "finished": len(permits)})
    except Exception as error:
        # Never copy URLs or backend exception messages into process results.
        result = {"worker": worker, "unexpected": type(error).__name__}
        admitted.put(result)
        finished.put(result)
    finally:
        if client is not None:
            client.close()


def _abandon_worker(url, namespace, result):
    """Exit with an unfinished permit, as a worker lost after admission would."""
    client = None
    try:
        client = _client(url)
        limiter = RedisRateLimiter(client, namespace=namespace, lease_seconds=0.3)
        limiter.acquire(_KEY, policy=RateLimitPolicy(max_concurrent=1), estimated_tokens=10,
                        provider="integration", model_id="logical-test")
        result.put("admitted")
    except Exception as error:
        result.put(type(error).__name__)
    finally:
        if client is not None:
            client.close()


def _ownership_worker(url, namespace, release, result):
    from taip.deployment.ownership import CoreOwnershipLease

    client, owner = None, None
    try:
        client = _client(url)
        owner = CoreOwnershipLease(client, namespace=namespace, lease_seconds=0.6)
        owner.start()
        result.put("owned")
        if not release.wait(20):
            raise TimeoutError
        result.put("current" if owner.is_current() else "lost")
    except Exception as error:
        result.put(type(error).__name__)
    finally:
        if owner is not None:
            owner.close()
        if client is not None:
            client.close()


class RealRedisProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This is the only environment variable these tests consult.
        cls.redis_url = os.environ.get("TAIP_TEST_REDIS_URL")
        if not cls.redis_url:
            raise unittest.SkipTest("TAIP_TEST_REDIS_URL is not configured")
        client = None
        try:
            client = _client(cls.redis_url)
            if client.ping() is not True:
                raise RuntimeError
        except Exception:
            raise AssertionError("Configured Redis integration server is unavailable.") from None
        finally:
            if client is not None:
                client.close()

    def setUp(self):
        self.context = multiprocessing.get_context("spawn")
        self.namespace = "taip:test:" + uuid4().hex
        self.client = _client(self.redis_url)
        self.addCleanup(self.client.close)
        self.addCleanup(self._delete_namespace)
        self.limiter = RedisRateLimiter(self.client, namespace=self.namespace)

    def _delete_namespace(self):
        prefix = (self.namespace + ":").encode("ascii")
        batch = []
        for key in self.client.scan_iter(match=prefix + b"*", count=128):
            if not key.startswith(prefix):
                raise AssertionError("Integration cleanup escaped its namespace.")
            batch.append(key)
            if len(batch) == 128:
                self.client.delete(*batch)
                batch.clear()
        if batch:
            self.client.delete(*batch)

    @staticmethod
    def _stop_process(process):
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()

    def _queue(self):
        queue = self.context.Queue()
        self.addCleanup(queue.close)
        return queue

    def _start_process(self, target, args):
        process = self.context.Process(target=target, args=args)
        process.start()
        self.addCleanup(self._stop_process, process)
        return process

    def _receive(self, queue):
        try:
            return queue.get(timeout=15)
        except Empty:
            self.fail("Redis integration worker did not report within its deadline.")

    @contextmanager
    def workers(self, *, count=6, attempts=3, shared_id=None, cooldown=None, **changes):
        values = dict(requests_per_minute=100, requests_per_day=200,
                      tokens_per_minute=1000, max_concurrent=50)
        values.update(changes)
        ready, admitted, finished = self._queue(), self._queue(), self._queue()
        start, release = self.context.Event(), self.context.Event()
        self.addCleanup(release.set)
        processes = [self._start_process(
            _quota_worker, (self.redis_url, self.namespace, index, attempts, values,
                            shared_id, cooldown, ready, start, release, admitted, finished),
        ) for index in range(count)]
        try:
            self.assertEqual({self._receive(ready) for _ in processes}, set(range(count)))
            start.set()
            results = [self._receive(admitted) for _ in processes]
            self.assertTrue(all("unexpected" not in result for result in results), results)
            yield results
        finally:
            start.set()
            release.set()
            for process in processes:
                process.join(timeout=15)
        completions = [self._receive(finished) for _ in processes]
        self.assertTrue(all("unexpected" not in result for result in completions), completions)
        self.assertTrue(all(process.exitcode == 0 for process in processes),
                        "Redis integration worker failed to exit cleanly.")

    def counts(self):
        snapshot = self.limiter.snapshot(_KEY)
        return (snapshot.requests_in_minute, snapshot.requests_in_day,
                snapshot.tokens_in_minute, snapshot.in_flight)

    def test_request_limit_has_exact_shared_admissions_and_finish_is_idempotent(self):
        with self.workers(attempts=8, requests_per_minute=7) as results:
            self.assertEqual(sum(result["accepted"] for result in results), 7)
            self.assertEqual(sum(result["denied"].get("local_rate_limit", 0)
                                 for result in results), 41)
            self.assertTrue(all(set(result["denied"]) <= {"local_rate_limit"} for result in results))
            self.assertEqual(self.counts(), (7, 7, 70, 7))
        self.assertEqual(self.counts(), (7, 7, 21, 0))

    def test_concurrency_capacity_is_atomic_across_independent_processes(self):
        with self.workers(max_concurrent=3) as results:
            self.assertEqual(sum(result["accepted"] for result in results), 3)
            self.assertEqual(sum(result["denied"].get("local_rate_limit", 0)
                                 for result in results), 15)
            self.assertEqual(self.counts(), (3, 3, 30, 3))
        self.assertEqual(self.counts(), (3, 3, 9, 0))

    def test_shared_acquisition_id_and_competing_finish_charge_only_once(self):
        with self.workers(attempts=2, shared_id="same-logical-admission") as results:
            self.assertEqual(sum(result["accepted"] for result in results), 12)
            self.assertTrue(all(result["denied"] == {} for result in results))
            self.assertEqual(self.counts(), (1, 1, 10, 1))
        self.assertEqual(self.counts(), (1, 1, 3, 0))

    def test_cooldown_from_one_process_blocks_another_without_extending_on_duplicate_finish(self):
        with self.workers(count=1, attempts=1, cooldown=20):
            self.assertEqual(self.counts(), (1, 1, 10, 1))
        self.assertEqual(self.counts(), (1, 1, 3, 0))
        with self.assertRaises(AIProviderError) as caught:
            self.limiter.acquire(_KEY, policy=RateLimitPolicy(
                requests_per_minute=100, requests_per_day=200,
                tokens_per_minute=1000, max_concurrent=50,
            ), estimated_tokens=10, provider="integration", model_id="logical-test")
        self.assertEqual(caught.exception.code, "local_rate_limit")
        self.assertGreater(caught.exception.retry_after_seconds, 0)
        self.assertLessEqual(caught.exception.retry_after_seconds, 20)
        self.assertEqual(self.counts(), (1, 1, 3, 0))

    def test_exited_worker_reservation_recovers_only_after_redis_lease_expires(self):
        result = self._queue()
        process = self._start_process(_abandon_worker, (self.redis_url, self.namespace, result))
        self.assertEqual(self._receive(result), "admitted")
        process.join(timeout=15)
        self.assertEqual(process.exitcode, 0)
        limiter = RedisRateLimiter(self.client, namespace=self.namespace, lease_seconds=0.3)
        self.assertEqual(limiter.snapshot(_KEY).requests_in_day, 1)
        deadline = time.monotonic() + 5
        while True:
            try:
                permit = limiter.acquire(_KEY, policy=RateLimitPolicy(max_concurrent=1),
                                         estimated_tokens=5, provider="integration", model_id="logical-test")
                break
            except AIProviderError as error:
                self.assertEqual(error.code, "local_rate_limit")
                if time.monotonic() >= deadline:
                    self.fail("Expired Redis lease did not release worker capacity.")
                time.sleep(0.02)
        permit.finish(actual_tokens=0)
        snapshot = limiter.snapshot(_KEY)
        self.assertEqual((snapshot.requests_in_day, snapshot.tokens_in_minute, snapshot.in_flight),
                         (2, 10, 0))

    def test_only_one_process_owns_the_core_and_heartbeat_preserves_its_lease(self):
        from taip.deployment.ownership import CoreOwnershipLease

        release, result = self.context.Event(), self._queue()
        process = self._start_process(_ownership_worker,
                                      (self.redis_url, self.namespace, release, result))
        self.addCleanup(release.set)
        self.assertEqual(self._receive(result), "owned")
        # More than one complete Redis TTL elapses while the independent worker
        # renews ownership. A second core must remain excluded throughout it.
        time.sleep(0.9)
        competitor = CoreOwnershipLease(self.client, namespace=self.namespace)
        self.addCleanup(competitor.close)
        with self.assertRaises(RuntimeError):
            competitor.start()
        release.set()
        self.assertEqual(self._receive(result), "current")
        process.join(timeout=15)
        self.assertEqual(process.exitcode, 0)
        replacement = CoreOwnershipLease(self.client, namespace=self.namespace)
        self.addCleanup(replacement.close)
        replacement.start()
        self.assertTrue(replacement.is_current())
        self.assertFalse(competitor.is_current())


if __name__ == "__main__":
    unittest.main()
