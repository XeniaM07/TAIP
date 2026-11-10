"""Public single-team authentication and ambient browser credential boundaries."""

import base64
from dataclasses import FrozenInstanceError
import hashlib
import importlib
from importlib import util
import unittest


TOKEN = "public_test_token_" + "a" * 32
OTHER_TOKEN = "other_test_token_" + "b" * 32
DIGEST = hashlib.sha256(TOKEN.encode("ascii")).digest()
ORIGIN = "https://lab.example.test"


def bearer(token=TOKEN):
    return (b"authorization", b"Bearer " + token.encode("ascii"))


def basic(username="taip", token=TOKEN):
    return (b"authorization", b"Basic " + base64.b64encode(f"{username}:{token}".encode("utf8")))


class CredentialVerifierTests(unittest.TestCase):
    def setUp(self):
        name = "taip.deployment.auth"
        self.assertIsNotNone(util.find_spec(name), "Deployment authentication module must exist")
        self.module = importlib.import_module(name)
        self.verifier = self.module.CredentialVerifier(DIGEST)

    def test_public_gate_accepts_valid_bearer_and_browser_basic(self):
        for headers, expected in (([bearer()], "bearer"), ([basic()], "basic")):
            with self.subTest(expected=expected):
                self.assertEqual(self.verifier.authenticate(headers), expected)

    def test_credential_verifier_hides_digest_and_cannot_be_mutated(self):
        self.assertNotIn(repr(DIGEST), repr(self.verifier))
        self.assertFalse(hasattr(self.verifier, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            self.verifier.basic_username = "changed"

    def test_verifier_rejects_invalid_configuration_without_private_messages(self):
        for value in (None, b"short", DIGEST.hex(), bytearray(DIGEST)):
            with self.subTest(kind=type(value)), self.assertRaises(ValueError) as caught:
                self.module.CredentialVerifier(value)
            self.assertNotIn("short", str(caught.exception))
        for value in (1, "true", None):
            with self.subTest(kind=type(value)), self.assertRaises(ValueError):
                self.module.CredentialVerifier(DIGEST, allow_basic=value)

    def test_internal_gate_accepts_only_bearer_and_uses_its_separate_digest(self):
        internal = self.module.CredentialVerifier(hashlib.sha256(OTHER_TOKEN.encode("ascii")).digest(), allow_basic=False)
        self.assertEqual(internal.authenticate([bearer(OTHER_TOKEN)]), "bearer")
        for headers in ([bearer()], [basic(token=OTHER_TOKEN)], [basic()]):
            with self.subTest(kind=headers[0][1][:5]):
                self.assertIsNone(internal.authenticate(headers))

    def test_authorization_name_and_scheme_are_case_insensitive(self):
        self.assertEqual(self.verifier.authenticate([(b"AUTHORIZATION", b"bEaReR " + TOKEN.encode())]), "bearer")
        self.assertEqual(self.verifier.authenticate([(b"Authorization", basic()[1].replace(b"Basic", b"bAsIc"))]), "basic")

    def test_duplicate_authorization_headers_are_rejected_even_when_identical(self):
        for headers in ([bearer(), bearer()], [bearer(), basic()], [basic(), basic()],
                        [bearer(), (b"Authorization", bearer()[1])]):
            with self.subTest(count=len(headers)):
                self.assertIsNone(self.verifier.authenticate(headers))

    def test_query_cookie_and_unrelated_headers_never_authenticate(self):
        for headers in ([], [(b"cookie", b"access_token=" + TOKEN.encode())],
                        [(b"x-api-key", TOKEN.encode())], [(b"?token", TOKEN.encode())]):
            with self.subTest(headers_count=len(headers)):
                self.assertIsNone(self.verifier.authenticate(headers))

    def test_invalid_bearer_syntax_and_unmatched_tokens_are_rejected(self):
        for value in (
            b"Bearer", b"Bearer ", b" Bearer " + TOKEN.encode(),
            b"Bearer  " + TOKEN.encode(), b"Bearer\t" + TOKEN.encode(),
            b"Bearer " + TOKEN.encode() + b" ", b"Bearer " + TOKEN.encode() + b"\r\n",
            b"Bearer " + b"a" * 42, b"Bearer " + b"a" * 257,
            b"Bearer " + b"a" * 43 + b"=", b"Bearer " + b"\xff" * 44,
            b"Bearer " + OTHER_TOKEN.encode(), b"Digest " + TOKEN.encode(), b"x" * 4097,
        ):
            with self.subTest(length=len(value)):
                self.assertIsNone(self.verifier.authenticate([(b"authorization", value)]))

    def test_basic_requires_valid_base64_utf8_username_and_token(self):
        for value in (
            b"Basic", b"Basic ", b"Basic !!!!", b"Basic //8=", b"Basic Zm9v",
            b"Basic " + base64.b64encode(b":" + TOKEN.encode()),
            b"Basic " + base64.b64encode(b"taip:"),
            b"Basic " + base64.b64encode(b"taip:" + TOKEN.encode() + b":other"),
            basic(username="wrong")[1], basic(token=OTHER_TOKEN)[1],
            basic(token="short")[1], basic()[1] + b"\n", b"Basic " + b"A" * 4096,
        ):
            with self.subTest(length=len(value)):
                self.assertIsNone(self.verifier.authenticate([(b"authorization", value)]))

    def test_configured_username_supports_utf8_and_must_match(self):
        verifier = self.module.CredentialVerifier(DIGEST, basic_username="echipă")
        self.assertEqual(verifier.authenticate([basic(username="echipă")]), "basic")
        self.assertIsNone(verifier.authenticate([basic(username="echipa")]))

    def test_malformed_header_shapes_return_none_without_exceptions(self):
        for headers in (None, [None], [(b"authorization",)], [(b"authorization", b"a", b"b")],
                        [("authorization", bearer()[1])], [(b"authorization", "Bearer token")],
                        [42], [bearer(), (b"broken", None)]):
            with self.subTest(kind=type(headers)):
                self.assertIsNone(self.verifier.authenticate(headers))


class OriginPolicyTests(unittest.TestCase):
    def setUp(self):
        name = "taip.deployment.auth"
        self.assertIsNotNone(util.find_spec(name), "Deployment authentication module must exist")
        self.module = importlib.import_module(name)

    def allowed(self, method="GET", headers=None, scheme="https", expected_origin=ORIGIN):
        return self.module.origin_is_allowed(method, [] if headers is None else headers,
                                             scheme=scheme, expected_origin=expected_origin)

    def test_originless_basic_allows_safe_navigation_but_requires_tls(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.assertTrue(self.allowed(method, [basic()]))
                self.assertFalse(self.allowed(method, [basic()], scheme="http"))

    def test_ambient_basic_cannot_mutate_without_a_matching_origin(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertFalse(self.allowed(method, [basic()]))
                self.assertTrue(self.allowed(method, [basic(), (b"origin", ORIGIN.encode())]))
                self.assertFalse(self.allowed(method, [basic(), (b"origin", b"https://attacker.test")]))

    def test_originless_bearer_supports_cli_mutations(self):
        for method in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertTrue(self.allowed(method, [bearer()]))
                self.assertFalse(self.allowed(method, [bearer()], scheme="http"))

    def test_any_supplied_invalid_or_cross_origin_is_rejected_for_every_auth_scheme(self):
        for credential in (bearer(), basic()):
            for origin in (
                b"null", b"", b"https://attacker.test", b"http://lab.example.test",
                b"https://lab.example.test/", b"https://user:pw@lab.example.test",
                b"https://lab.example.test?", b"https://lab.example.test#fragment",
                b"https://lab.example.test https://attacker.test", b"\xff",
                b"https://lab.example.test\n", b" https://lab.example.test",
            ):
                with self.subTest(credential=credential[1][:5], length=len(origin)):
                    self.assertFalse(self.allowed(headers=[credential, (b"origin", origin)]))

    def test_duplicate_origins_are_rejected_and_origin_header_name_is_case_insensitive(self):
        self.assertTrue(self.allowed(headers=[bearer(), (b"Origin", ORIGIN.encode())]))
        self.assertFalse(self.allowed(headers=[bearer(), (b"origin", ORIGIN.encode()), (b"ORIGIN", ORIGIN.encode())]))

    def test_canonical_origin_comparison_handles_hostname_case_and_default_port(self):
        self.assertTrue(self.allowed("POST", [basic(), (b"origin", b"https://LAB.example.test:443")]))
        self.assertFalse(self.allowed("POST", [basic(), (b"origin", b"https://lab.example.test:444")]))

    def test_untrusted_forwarding_and_host_headers_do_not_replace_origin_or_scheme(self):
        forwarded = [(b"x-forwarded-proto", b"https"), (b"x-forwarded-host", b"lab.example.test"),
                     (b"host", b"lab.example.test")]
        self.assertFalse(self.allowed("POST", [basic(), *forwarded]))
        self.assertFalse(self.allowed("GET", [basic(), *forwarded], scheme="http"))

    def test_cookie_navigation_and_mutations_obey_ambient_origin_policy(self):
        def allowed(method, headers=(), scheme="https"):
            try:
                return self.module.origin_is_allowed(method, headers, scheme=scheme,
                    expected_origin=ORIGIN, authentication_kind="cookie")
            except TypeError:
                self.fail("Origin policy must support authenticated browser cookies.")
        self.assertTrue(allowed("GET"))
        self.assertFalse(allowed("GET", scheme="http"))
        self.assertFalse(allowed("GET", [(b"origin", b"https://attacker.test")]))
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertFalse(allowed(method))
                self.assertTrue(allowed(method, [(b"origin", ORIGIN.encode())]))
                for origin in (b"null", b"https://attacker.test"):
                    self.assertFalse(allowed(method, [(b"origin", origin)]))

    def test_bad_inputs_return_false_without_exceptions(self):
        for headers in ([None], [(b"origin", "https://lab.example.test")], [bearer(), bearer()]):
            with self.subTest(kind=type(headers)):
                self.assertFalse(self.allowed(headers=headers))
        for value in (None, "", "not-an-origin", "http://lab.example.test"):
            with self.subTest(expected=value):
                self.assertFalse(self.allowed(headers=[bearer()], expected_origin=value))
        self.assertFalse(self.allowed(method=None, headers=[basic()]))


if __name__ == "__main__":
    unittest.main()
