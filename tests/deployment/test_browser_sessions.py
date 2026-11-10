"""Browser credentials use real Lua, bounded state, and synthetic secrets only."""

from concurrent.futures import ThreadPoolExecutor
from importlib import import_module, util
import hashlib
import os
import re
import secrets
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import fakeredis


ORIGIN = "https://localhost:8443"
DIGEST = hashlib.sha256(b"synthetic-application-password").digest()
TOKEN = "a" * 43
COOKIE_NAME = "__Host-taip_session_0123456789abcdef"


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        name = "taip.deployment.browser_sessions"
        self.assertIsNotNone(util.find_spec(name), "The browser session backend must exist")
        self.module = import_module(name)
        self.now = 1_000_000.0
        timer = patch("fakeredis.commands_mixins.server_mixin.time.time", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.server = fakeredis.FakeServer()
        self.client = fakeredis.FakeRedis(server=self.server)
        self.addCleanup(self.client.close)
        self.store = self.make_store()

    def make_store(self, client=None, **changes):
        arguments = dict(namespace="taip:browser-test", public_origin=ORIGIN, access_token_digest=DIGEST)
        arguments.update(changes)
        return self.module.RedisBrowserSessions(self.client if client is None else client, **arguments)

    def key(self, suffix):
        found = [key for key in self.client.keys() if key.endswith(suffix.encode())]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_one_use_exchange_authenticates_across_independent_instances(self):
        other_client = fakeredis.FakeRedis(server=self.server)
        self.addCleanup(other_client.close)
        other = self.make_store(other_client)
        code = self.store.mint()
        self.assertRegex(code, r"^[A-Za-z0-9_-]{43}$")
        self.assertFalse(other.authenticate(code))
        session = other.exchange(code)
        self.assertRegex(session, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotEqual(code, session)
        self.assertTrue(self.store.authenticate(session))
        self.assertIsNone(self.store.exchange(code))

    def test_parallel_exchange_issues_exactly_one_session(self):
        clients = [fakeredis.FakeRedis(server=self.server) for _ in range(4)]
        for client in clients:
            self.addCleanup(client.close)
        stores = [self.make_store(client) for client in clients]
        code = self.store.mint()
        with ThreadPoolExecutor(max_workers=8) as executor:
            sessions = list(executor.map(lambda index: stores[index % 4].exchange(code), range(24)))
        accepted = [session for session in sessions if session is not None]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(self.store.authenticate(accepted[0]))

    def test_code_expires_at_120_seconds_without_later_mints_extending_it(self):
        old = self.store.mint()
        self.now += 119
        newer = self.store.mint()
        self.now += 1
        self.assertIsNone(self.store.exchange(old))
        self.assertIsNotNone(self.store.exchange(newer))

    def test_session_expires_at_12_hours_without_authentication_renewal(self):
        session = self.store.exchange(self.store.mint())
        self.now += 43199
        self.assertTrue(self.store.authenticate(session))
        self.now += 1
        self.assertFalse(self.store.authenticate(session))

    def test_invalid_tokens_do_not_allocate_or_access_backend_state(self):
        self.server.connected = False
        for value in (None, b"x" * 43, "", "a" * 42, "a" * 44, "a" * 42 + "=", "a" * 42 + "\n", "ă" * 43):
            with self.subTest(kind=type(value).__name__):
                self.assertIsNone(self.store.exchange(value))
                self.assertFalse(self.store.authenticate(value))

    def test_namespace_origin_and_password_rotation_isolate_codes_and_sessions(self):
        code = self.store.mint()
        session = self.store.exchange(self.store.mint())
        for changes in (
            {"namespace": "taip:another"}, {"public_origin": "https://localhost:9443"},
            {"access_token_digest": hashlib.sha256(b"rotated-password").digest()},
        ):
            with self.subTest(change=next(iter(changes))):
                other = self.make_store(**changes)
                self.assertIsNone(other.exchange(code))
                self.assertFalse(other.authenticate(session))
        same = self.make_store(public_origin="https://LOCALHOST:8443")
        self.assertTrue(same.authenticate(session))
        self.assertIsNotNone(same.exchange(code))

    def test_cookie_name_is_origin_bound_and_stable_after_password_rotation(self):
        self.assertRegex(self.store.cookie_name, r"^__Host-taip_session_[a-f0-9]{16}$")
        same_origin = self.make_store(access_token_digest=hashlib.sha256(b"rotated").digest())
        other_origin = self.make_store(public_origin="https://localhost:9443")
        self.assertEqual(self.store.cookie_name, same_origin.cookie_name)
        self.assertNotEqual(self.store.cookie_name, other_origin.cookie_name)

    def test_pending_codes_evict_the_oldest_at_eight_and_sessions_remain_usable(self):
        session = self.store.exchange(self.store.mint())
        codes = []
        for _ in range(9):
            codes.append(self.store.mint())
            self.now += 0.01
        self.assertEqual(self.client.zcard(self.key(":codes")), 8)
        self.assertIsNone(self.store.exchange(codes[0]))
        self.assertIsNotNone(self.store.exchange(codes[1]))
        self.assertTrue(self.store.authenticate(session))

    def test_session_capacity_evicts_oldest_and_uses_only_two_fixed_hashed_sets(self):
        sessions = []
        codes = []
        for _ in range(129):
            code = self.store.mint()
            codes.append(code)
            sessions.append(self.store.exchange(code))
            self.now += 0.01
        pending = self.store.mint()
        self.assertFalse(self.store.authenticate(sessions[0]))
        self.assertTrue(self.store.authenticate(sessions[1]))
        self.assertTrue(self.store.authenticate(sessions[-1]))
        self.assertEqual(len(self.client.keys()), 2)
        self.assertEqual(self.client.zcard(self.key(":sessions")), 128)
        keys = self.client.keys()
        self.assertEqual(len({re.search(rb"\{([^}]+)\}", key).group(1) for key in keys}), 1)
        for key in keys:
            self.assertGreater(self.client.pttl(key), 0)
            members = self.client.zrange(key, 0, -1)
            self.assertTrue(all(re.fullmatch(rb"[a-f0-9]{64}", member) for member in members))
            saved = key + b" ".join(members)
            for raw in (*codes, *sessions, pending):
                self.assertNotIn(raw.encode(), saved)
        self.assertLessEqual(self.client.pttl(self.key(":codes")), 120000)
        self.assertLessEqual(self.client.pttl(self.key(":sessions")), 43200000)

    def test_wrong_redis_types_fail_closed_without_overwriting_state(self):
        code = self.store.mint()
        key = self.key(":codes")
        self.client.delete(key)
        self.client.set(key, b"synthetic-corrupt-private-value")
        with self.assertRaises(self.module.BrowserSessionUnavailable) as caught:
            self.store.exchange(code)
        self.assertNotIn(code, str(caught.exception))
        self.assertNotIn("synthetic-corrupt-private-value", repr(caught.exception))
        self.assertEqual(self.client.get(key), b"synthetic-corrupt-private-value")

    def test_corrupt_members_future_scores_and_excessive_cardinality_fail_closed(self):
        for scenario in ("member", "score", "capacity"):
            with self.subTest(scenario=scenario):
                self.client.flushdb()
                session = self.store.exchange(self.store.mint())
                key = self.key(":sessions")
                if scenario == "member":
                    self.client.zadd(key, {"not-a-digest": self.now * 1000 + 1000})
                elif scenario == "score":
                    self.client.zadd(key, {"a" * 64: self.now * 1000 + 43200001})
                else:
                    self.client.zadd(key, {f"{index:064x}": self.now * 1000 + 1000 for index in range(129)})
                before = self.client.dump(key)
                with self.assertRaises(self.module.BrowserSessionUnavailable):
                    self.store.authenticate(session)
                self.assertEqual(self.client.dump(key), before)

    def test_backend_failures_and_malformed_responses_never_authenticate_or_leak(self):
        self.server.connected = False
        for operation in (self.store.mint, lambda: self.store.exchange(TOKEN), lambda: self.store.authenticate(TOKEN)):
            with self.assertRaises(self.module.BrowserSessionUnavailable) as caught:
                operation()
            self.assertNotIn(TOKEN, repr(caught.exception))
        self.server.connected = True
        for response in (None, True, b"1", [1], 2, -1):
            with self.subTest(response=type(response).__name__), patch.object(self.client, "eval", return_value=response):
                with self.assertRaises(self.module.BrowserSessionUnavailable):
                    self.store.authenticate(TOKEN)

    def test_repeated_random_collision_is_bounded_and_does_not_renew_existing_code(self):
        with patch.object(self.module.secrets, "token_urlsafe", return_value=TOKEN):
            self.assertEqual(self.store.mint(), TOKEN)
            self.now += 10
            before = self.client.pttl(self.key(":codes"))
            with self.assertRaises(self.module.BrowserSessionUnavailable):
                self.store.mint()
            self.assertEqual(self.client.pttl(self.key(":codes")), before)

    def test_exchange_never_reuses_the_handoff_code_as_a_session_credential(self):
        code = self.store.mint()
        with patch.object(self.module.secrets, "token_urlsafe", side_effect=[code, "B" * 43]):
            session = self.store.exchange(code)
        self.assertEqual(session, "B" * 43)
        self.assertFalse(self.store.authenticate(code))
        self.assertTrue(self.store.authenticate(session))

    def test_session_collision_does_not_consume_code_or_renew_old_session(self):
        session = self.store.exchange(self.store.mint())
        code = self.store.mint()
        self.now += 5
        before = self.client.dump(self.key(":sessions"))
        with patch.object(self.module.secrets, "token_urlsafe", return_value=session):
            with self.assertRaises(self.module.BrowserSessionUnavailable):
                self.store.exchange(code)
        self.assertEqual(self.client.dump(self.key(":sessions")), before)
        self.assertIsNotNone(self.store.exchange(code))

    def test_constructor_rejects_unsafe_configuration_without_echoing_values(self):
        for change in (
            {"namespace": "private-value{bad}"}, {"namespace": "other:private-value"},
            {"public_origin": "http://private-value"}, {"public_origin": "https://localhost/private-value"},
            {"access_token_digest": b"private-value"},
        ):
            with self.subTest(field=next(iter(change))), self.assertRaises(ValueError) as caught:
                self.make_store(**change)
            self.assertNotIn("private-value", str(caught.exception))


class BrowserCookieTests(unittest.TestCase):
    def setUp(self):
        name = "taip.deployment.browser_sessions"
        self.assertIsNotNone(util.find_spec(name), "Strict browser cookie parsing must exist")
        self.read = import_module(name).read_browser_cookie

    def test_exact_cookie_among_unrelated_cookies_and_split_headers_is_accepted(self):
        headers = [(b"Cookie", b"other=one"), (b"cookie", COOKIE_NAME.encode() + b"=" + TOKEN.encode() + b"; another=two")]
        self.assertEqual(self.read(headers, COOKIE_NAME), TOKEN)
        self.assertIsNone(self.read([(b"cookie", b"unrelated=one")], COOKIE_NAME))
        self.assertIsNone(self.read([], COOKIE_NAME))

    def test_duplicate_session_cookies_are_rejected_even_when_identical(self):
        value = COOKIE_NAME.encode() + b"=" + TOKEN.encode()
        for headers in ([(b"cookie", value + b"; " + value)], [(b"cookie", value), (b"Cookie", value)]):
            self.assertIsNone(self.read(headers, COOKIE_NAME))

    def test_malformed_non_ascii_oversized_or_wrong_length_cookies_fail_closed(self):
        prefix = COOKIE_NAME.encode() + b"="
        for value in (prefix + b"a" * 42, prefix + b"a" * 44, prefix + b'"' + TOKEN.encode() + b'"',
                      prefix + TOKEN.encode() + b"=", prefix + TOKEN.encode() + b"\n",
                      prefix + TOKEN.encode() + b"; broken", prefix + TOKEN.encode() + b"; bad name=value",
                      prefix + TOKEN.encode() + b"; other=\xff", prefix + TOKEN.encode() + b"; other=" + b"x" * 8192):
            with self.subTest(length=len(value)):
                self.assertIsNone(self.read([(b"cookie", value)], COOKIE_NAME))
        for headers in (None, [None], [(b"cookie",)], [("cookie", "value")], [(b"cookie", None)],
                        [(b"cookie", prefix + TOKEN.encode())] * 101):
            self.assertIsNone(self.read(headers, COOKIE_NAME))


class RealRedisBrowserSessionTests(unittest.TestCase):
    """Opt-in disposable Redis; cleanup is restricted to a fresh test namespace."""

    @classmethod
    def setUpClass(cls):
        cls.redis_url = os.environ.get("TAIP_TEST_REDIS_URL")
        if not cls.redis_url:
            raise unittest.SkipTest("TAIP_TEST_REDIS_URL is not configured")

    def setUp(self):
        from tests.deployment.test_redis_processes import _client, _safe_redis_call
        self.namespace = "taip:test:browser:" + uuid4().hex
        self.client = _client(self.redis_url)
        self.addCleanup(self.client.close)
        self.addCleanup(self.delete_own_namespace)
        _safe_redis_call("Browser integration ping", self.client.ping)
        self.module = import_module("taip.deployment.browser_sessions")
        self.store = self.module.RedisBrowserSessions(self.client, namespace=self.namespace,
                        public_origin=ORIGIN, access_token_digest=DIGEST)

    def delete_own_namespace(self, client=None):
        from tests.deployment.test_redis_processes import _safe_redis_call
        client = self.client if client is None else client
        def cleanup():
            prefix = (self.namespace + ":").encode()
            keys = list(client.scan_iter(match=prefix + b"*", count=128))
            if any(not key.startswith(prefix) for key in keys):
                raise AssertionError("Browser integration cleanup escaped its namespace")
            if keys:
                client.delete(*keys)
        _safe_redis_call("Browser integration cleanup", cleanup)

    def test_real_concurrent_clients_exchange_once_and_rotation_invalidates_session(self):
        from tests.deployment.test_redis_processes import _client
        clients = [_client(self.redis_url) for _ in range(4)]
        for client in clients:
            self.addCleanup(client.close)
        stores = [self.module.RedisBrowserSessions(client, namespace=self.namespace,
                    public_origin=ORIGIN, access_token_digest=DIGEST) for client in clients]
        code = self.store.mint()
        with ThreadPoolExecutor(max_workers=8) as executor:
            sessions = list(executor.map(lambda index: stores[index % 4].exchange(code), range(24)))
        accepted = [session for session in sessions if session is not None]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(self.store.authenticate(accepted[0]))
        rotated = self.module.RedisBrowserSessions(self.client, namespace=self.namespace,
                    public_origin=ORIGIN, access_token_digest=hashlib.sha256(b"rotated").digest())
        self.assertFalse(rotated.authenticate(accepted[0]))

    def test_real_expiry_capacity_and_wrong_types_are_enforced(self):
        sessions = [self.store.exchange(self.store.mint()) for _ in range(129)]
        keys = list(self.client.scan_iter(match=self.namespace + ":*"))
        session_key = next(key for key in keys if key.endswith(b":sessions"))
        self.assertEqual(self.client.zcard(session_key), 128)
        self.assertTrue(self.store.authenticate(sessions[-1]))
        self.client.zadd(session_key, {hashlib.sha256(sessions[-1].encode()).hexdigest(): 1})
        self.assertFalse(self.store.authenticate(sessions[-1]))
        self.client.delete(session_key)
        self.client.set(session_key, b"corrupt-state")
        with self.assertRaises(self.module.BrowserSessionUnavailable):
            self.store.authenticate(sessions[0])

    def test_real_browser_store_works_with_the_unchanged_generated_deployment_acl(self):
        import redis
        from pathlib import Path
        from taip.deployment.setup_public import prepare_deployment
        from tests.deployment.test_redis_processes import _safe_redis_call

        with TemporaryDirectory(prefix="taip synthetic browser ACL ") as temporary:
            directory = Path(temporary) / "private"
            prepare_deployment(directory)
            declaration = next(line.split() for line in (directory / "redis.acl").read_text().splitlines()
                               if line.startswith("user taip "))
            rules = [part for part in declaration[2:] if part.startswith(("+", "-"))]
        username = "taip_browser_test_" + uuid4().hex
        password = secrets.token_urlsafe(48)
        self.addCleanup(_safe_redis_call, "Browser ACL cleanup", self.client.execute_command,
                        "ACL", "DELUSER", username)
        _safe_redis_call("Browser ACL creation", self.client.execute_command,
                        "ACL", "SETUSER", username, "reset", "on", ">" + password,
                        "~" + self.namespace + ":*", *rules)

        source = self.client.connection_pool
        admin_options = {**source.connection_kwargs, "db": 0}
        admin_pool = redis.ConnectionPool(connection_class=source.connection_class, **admin_options)
        self.addCleanup(admin_pool.disconnect)
        admin = redis.Redis(connection_pool=admin_pool)
        self.addCleanup(admin.close)
        self.addCleanup(self.delete_own_namespace, admin)
        options = {key: value for key, value in admin_options.items()
                   if key not in {"username", "password", "credential_provider"}}
        pool = redis.ConnectionPool(connection_class=source.connection_class,
                                    username=username, password=password, **options)
        self.addCleanup(pool.disconnect)
        restricted = redis.Redis(connection_pool=pool)
        self.addCleanup(restricted.close)
        store = self.module.RedisBrowserSessions(restricted, namespace=self.namespace,
                    public_origin=ORIGIN, access_token_digest=DIGEST)
        code = store.mint()
        session = store.exchange(code)
        self.assertIsNotNone(session)
        self.assertTrue(store.authenticate(session))
        self.assertIsNone(store.exchange(code))


if __name__ == "__main__":
    unittest.main()
