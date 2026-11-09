"""Redis ownership uses a shared offline server and deterministic fault injection."""

import importlib
from importlib import util
import time
import unittest

import fakeredis


class CoreOwnershipTests(unittest.TestCase):
    def setUp(self):
        name = "taip.deployment.ownership"
        self.assertIsNotNone(util.find_spec(name), "Core ownership lease module must exist")
        self.module = importlib.import_module(name)
        self.server = fakeredis.FakeServer()
        self.client = fakeredis.FakeRedis(server=self.server)
        self.other_client = fakeredis.FakeRedis(server=self.server)
        self.owners = []

    def tearDown(self):
        for owner in self.owners:
            owner.close()
        self.client.close()
        self.other_client.close()

    def owner(self, client=None, **changes):
        owner = self.module.CoreOwnershipLease(self.client if client is None else client,
                                               namespace="taip:controlled", **changes)
        self.owners.append(owner)
        return owner

    def key(self):
        keys = self.client.keys("taip:controlled:*")
        self.assertEqual(len(keys), 1)
        return keys[0]

    def test_only_one_core_can_own_a_shared_namespace_until_owner_closes(self):
        first = self.owner()
        second = self.owner(self.other_client)
        self.assertFalse(first.is_current())
        first.start()
        self.assertTrue(first.is_current())
        self.assertGreater(self.client.pttl(self.key()), 0)
        with self.assertRaises(RuntimeError) as caught:
            second.start()
        self.assertNotIn("taip:controlled", str(caught.exception))
        first.close()
        fresh = self.owner(self.other_client)
        fresh.start()
        self.assertTrue(fresh.is_current())

    def test_started_owner_rechecks_ownership_without_creating_a_second_heartbeat(self):
        owner = self.owner()
        owner.start()
        thread = owner._thread
        owner.start()
        self.assertIs(owner._thread, thread)
        self.assertTrue(thread.daemon)

    def test_heartbeat_renews_current_lease_and_stops_on_close(self):
        owner = self.owner(lease_seconds=0.3)
        owner.start()
        key = self.key()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and self.client.pttl(key) > 170:
            time.sleep(0.01)
        low_ttl = self.client.pttl(key)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and self.client.pttl(key) <= low_ttl:
            time.sleep(0.01)
        self.assertGreater(self.client.pttl(key), low_ttl)
        self.assertTrue(owner.is_current())
        thread = owner._thread
        owner.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(owner.is_current())
        self.assertEqual(self.client.exists(key), 0)

    def test_lost_owner_cannot_delete_or_renew_the_replacement_or_restart(self):
        owner = self.owner()
        owner.start()
        key = self.key()
        self.other_client.set(key, b"replacement-owner", px=10000)
        self.assertFalse(owner.is_current())
        self.other_client.delete(key)
        self.assertFalse(owner.is_current())
        with self.assertRaises(RuntimeError):
            owner.start()
        self.other_client.set(key, b"replacement-owner", px=10000)
        owner.close()
        self.assertEqual(self.other_client.get(key), b"replacement-owner")

    def test_backend_failure_marks_loss_and_recovery_does_not_reuse_stale_state(self):
        owner = self.owner()
        owner.start()
        self.server.connected = False
        self.assertFalse(owner.is_current())
        self.server.connected = True
        self.assertFalse(owner.is_current())
        with self.assertRaises(RuntimeError):
            owner.start()

    def test_startup_redis_failure_has_fixed_safe_error(self):
        owner = self.owner()
        self.server.connected = False
        with self.assertRaises(RuntimeError) as caught:
            owner.start()
        self.assertNotIn("redis", str(caught.exception).lower())
        self.assertIsNone(caught.exception.__cause__)
        self.server.connected = True
        self.assertFalse(owner.is_current())

    def test_close_is_idempotent_and_never_closes_the_callers_redis_client(self):
        owner = self.owner()
        owner.start()
        owner.close()
        owner.close()
        self.assertTrue(self.client.ping())
        with self.assertRaises(RuntimeError):
            owner.start()

    def test_namespace_and_lease_validation_do_not_echo_input(self):
        for namespace in ("", "foreign:v1", "taip:bad value", "taip:é", "taip:" + "a" * 128, None):
            with self.subTest(kind=type(namespace)), self.assertRaises(ValueError) as caught:
                self.module.CoreOwnershipLease(self.client, namespace=namespace)
            self.assertNotIn("bad value", str(caught.exception))
        for value in (0, -1, True, "30", None, float("inf"), float("nan")):
            with self.subTest(kind=type(value)), self.assertRaises(ValueError):
                self.owner(lease_seconds=value)


if __name__ == "__main__":
    unittest.main()
