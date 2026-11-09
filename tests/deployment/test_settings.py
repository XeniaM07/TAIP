"""Deployment configuration checks use synthetic values and controlled readers only."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
import hashlib
import importlib
from importlib import util
import os
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch


PUBLIC_TOKEN = "public_test_token_" + "a" * 32
INTERNAL_TOKEN = "internal_test_token_" + "b" * 32
REDIS_PASSWORD = "redis_test_password_" + "c" * 32
DIGEST = hashlib.sha256(PUBLIC_TOKEN.encode("ascii")).digest()


def configuration_environment(**changes):
    values = {
        "TAIP_PUBLIC_ORIGIN": "https://lab.example.test",
        "TAIP_ACCESS_TOKEN_SHA256_FILE": "synthetic-public-digest",
        "TAIP_INTERNAL_TOKEN_FILE": "synthetic-internal-token",
        "TAIP_REDIS_PASSWORD_FILE": "synthetic-redis-password",
    }
    values.update(changes)
    return values


def synthetic_secret_reader(path):
    return {
        "synthetic-public-digest": DIGEST.hex(),
        "synthetic-internal-token": INTERNAL_TOKEN,
        "synthetic-redis-password": REDIS_PASSWORD,
    }[path]


class UnreadableEnvironment(Mapping):
    def __getitem__(self, key):
        raise AssertionError("Must not inspect the process environment")

    def __iter__(self):
        raise AssertionError("Must not inspect the process environment")

    def __len__(self):
        raise AssertionError("Must not inspect the process environment")


class DeploymentSettingsTests(unittest.TestCase):
    def setUp(self):
        name = "taip.deployment.settings"
        self.assertIsNotNone(util.find_spec(name), "Deployment settings module must exist")
        self.module = importlib.import_module(name)
        self.error = self.module.DeploymentConfigurationError

    def settings(self, **changes):
        values = {
            "public_origin": "https://lab.example.test",
            "access_token_digest": DIGEST,
            "internal_token": INTERNAL_TOKEN,
            "redis": self.module.RedisSettings(password=REDIS_PASSWORD),
        }
        values.update(changes)
        return self.module.DeploymentSettings(**values)

    def load(self, **changes):
        return self.module.load_deployment_settings(
            configuration_environment(**changes), secret_reader=synthetic_secret_reader
        )

    def test_settings_are_frozen_slotted_and_hide_credentials(self):
        settings = self.settings()
        for instance, field in ((settings, "public_origin"), (settings.redis, "host")):
            self.assertFalse(hasattr(instance, "__dict__"))
            with self.assertRaises(FrozenInstanceError):
                setattr(instance, field, "changed")
        representation = repr(settings)
        for private in (INTERNAL_TOKEN, REDIS_PASSWORD, repr(DIGEST), DIGEST.hex()):
            self.assertNotIn(private, representation)

    def test_valid_settings_normalize_origin_and_preserve_policy_defaults(self):
        settings = self.settings(public_origin="https://LAB.example.test:443")
        self.assertEqual(settings.public_origin, "https://lab.example.test")
        self.assertEqual(settings.redis.host, "redis")
        self.assertEqual(settings.redis.port, 6379)
        self.assertEqual(settings.redis.namespace, "taip:v1")
        self.assertEqual(settings.core_url, "http://core:8000")
        self.assertEqual(settings.web_url, "http://web:80")
        self.assertEqual(settings.gateway_workers, 2)
        self.assertIs(settings.live_ai, False)
        self.assertIs(settings.trust_tls_proxy, False)

    def test_origins_reject_credentials_paths_query_fragments_and_unsafe_schemes(self):
        for value in (
            "http://lab.example.test", "https://user:secret@lab.example.test",
            "https://lab.example.test/", "https://lab.example.test/path",
            "https://lab.example.test?", "https://lab.example.test#",
            "https://lab.example.test?q=1", "https://lab.example.test#part",
            "https://", "https://lab.example.test:0", "https://lab.example.test:65536",
            "https://lab.example.test:invalid", "https://lab.example.test\\other",
            "https://lab.example.test\n", " https://lab.example.test",
            "https://lab.example.test\t.attacker.test", "https://bad host.test",
            "https://lab.example.test.", "https://lab%2eexample.test", None, True,
        ):
            with self.subTest(value=value), self.assertRaises(self.error) as caught:
                self.settings(public_origin=value)
            self.assertEqual(caught.exception.field, "public_origin")

    def test_service_origins_allow_http_or_https_only_and_no_url_payload(self):
        for field in ("core_url", "web_url"):
            self.settings(**{field: "https://internal.example.test:8443"})
            for value in (
                "file:///tmp/core", "redis://core:6379", "http://user:pw@core",
                "http://core/api", "http://core?key=value", "http://core#fragment",
                "http://core\r\nx-injected:value", " core", "", None,
            ):
                with self.subTest(field=field, value=value), self.assertRaises(self.error):
                    self.settings(**{field: value})

    def test_policy_integer_fields_reject_coercion_booleans_and_nonpositive_values(self):
        for field in (
            "max_body_bytes", "max_response_bytes", "gateway_workers",
            "http_requests_per_minute", "http_requests_per_day", "http_max_concurrent",
        ):
            for value in (0, -1, True, False, 1.0, "2", None):
                with self.subTest(field=field, value=value), self.assertRaises(self.error) as caught:
                    self.settings(**{field: value})
                self.assertEqual(caught.exception.field, field)

    def test_body_and_response_caps_reject_unbounded_policy(self):
        for field in ("max_body_bytes", "max_response_bytes"):
            with self.subTest(field=field), self.assertRaises(self.error):
                self.settings(**{field: 16 * 1024 * 1024 + 1})

    def test_timeouts_are_finite_positive_numbers_and_lease_outlives_request(self):
        for field in ("body_timeout_seconds", "request_timeout_seconds", "redis_lease_seconds"):
            for value in (0, -1, True, False, float("inf"), float("nan"), "3", None):
                with self.subTest(field=field, value=value), self.assertRaises(self.error):
                    self.settings(**{field: value})
        for lease in (1, 300):
            with self.subTest(lease=lease), self.assertRaises(self.error):
                self.settings(redis_lease_seconds=lease)

    def test_secret_validation_rejects_padding_unicode_and_wrong_digest_types(self):
        for value in ("a" * 42, "a" * 257, "a" * 44 + "=", "é" * 44,
                      " " + INTERNAL_TOKEN, INTERNAL_TOKEN + "\n", None, True):
            with self.subTest(value=value), self.assertRaises(self.error) as caught:
                self.settings(internal_token=value)
            self.assertEqual(caught.exception.field, "internal_token")
        for value in (DIGEST.hex(), bytearray(DIGEST), b"x" * 31, b"x" * 33, None):
            with self.subTest(kind=type(value)), self.assertRaises(self.error):
                self.settings(access_token_digest=value)

    def test_credentials_require_distinct_public_internal_and_redis_identities(self):
        with self.assertRaises(self.error):
            self.settings(access_token_digest=hashlib.sha256(INTERNAL_TOKEN.encode("ascii")).digest())
        with self.assertRaises(self.error):
            self.settings(redis=self.module.RedisSettings(password=INTERNAL_TOKEN))

    def test_username_and_live_ai_require_valid_explicit_types(self):
        for value in ("", " ", " user", "user ", "user:password", "user\n", None, True):
            with self.subTest(value=value), self.assertRaises(self.error):
                self.settings(basic_username=value)
        for field in ("live_ai", "trust_tls_proxy"):
            for value in (0, 1, "true", None):
                with self.subTest(field=field, value=value), self.assertRaises(self.error):
                    self.settings(**{field: value})

    def test_redis_settings_validate_every_connection_component(self):
        for field, values in {
            "host": ("", " redis", "redis ", "redis://redis", "bad host", None),
            "port": (0, 65536, True, 6379.0, "6379"),
            "username": ("", " user", "user\n", None, True),
            "password": ("a" * 31, " " + REDIS_PASSWORD, REDIS_PASSWORD + "\n", "é" * 40, None),
            "namespace": ("", "other:v1", "taip: ", "taip:" + "a" * 128, "taip:é"),
            "tls": (1, "true", None),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(self.error):
                    self.module.RedisSettings(**{"password": REDIS_PASSWORD, field: value})

    def test_redis_configuration_must_be_validated_settings(self):
        for value in (None, {}, True, "redis"):
            with self.subTest(value=value), self.assertRaises(self.error):
                self.settings(redis=value)

    def test_redis_namespace_reserves_room_for_distributed_backend_suffixes(self):
        from taip.deployment.redis_limits import RedisRateLimiter

        redis = self.module.RedisSettings(password=REDIS_PASSWORD, namespace="taip:" + "a" * 85)
        RedisRateLimiter(object(), namespace=redis.namespace + ":providers")
        with self.assertRaises(self.error):
            self.module.RedisSettings(password=REDIS_PASSWORD, namespace="taip:" + "a" * 86)

    def test_redis_lease_policy_cannot_exceed_the_distributed_backends_supported_day(self):
        with self.assertRaises(self.error) as caught:
            self.settings(redis_lease_seconds=86400.001)
        self.assertEqual(caught.exception.field, "redis_lease_seconds")

    def test_http_permit_outlives_body_upstream_and_redis_settlement_deadlines(self):
        for lease in (310, 319, 320):
            with self.subTest(lease=lease):
                with self.assertRaises(self.error) as caught:
                    self.settings(redis_lease_seconds=lease)
                self.assertEqual(caught.exception.field, "redis_lease_seconds")
        self.settings(redis_lease_seconds=321)
        with self.assertRaises(self.error):
            self.settings(body_timeout_seconds=60, request_timeout_seconds=1, redis_lease_seconds=70)

    def test_configuration_errors_never_include_untrusted_input(self):
        private = "PRIVATE-input-never-show"
        with self.assertRaises(self.error) as caught:
            self.settings(public_origin=private)
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        for rendered in (str(error), repr(error), repr(vars(error))):
            self.assertNotIn(private, rendered)

    def test_host_errors_do_not_chain_untrusted_parser_diagnostics(self):
        private = "PRIVATE-input-never-show@"
        with self.assertRaises(self.error) as caught:
            self.module.RedisSettings(host=private, password=REDIS_PASSWORD)
        self.assertNotIn(private, "".join(traceback.format_exception(caught.exception)))

    def test_import_has_no_process_environment_reads(self):
        name = "taip.deployment._controlled_settings_import"
        spec = util.spec_from_file_location(name, self.module.__file__)
        isolated = util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: isolated}), patch.object(os, "environ", UnreadableEnvironment()):
            spec.loader.exec_module(isolated)

    def test_explicit_mapping_does_not_fall_back_to_process_environment(self):
        with patch.object(os, "environ", UnreadableEnvironment()):
            settings = self.load()
            self.assertEqual(settings.access_token_digest, DIGEST)
            with self.assertRaises(self.error):
                self.module.load_deployment_settings({}, secret_reader=synthetic_secret_reader)

    def test_environment_is_selected_at_call_time(self):
        with patch.object(os, "environ", configuration_environment()):
            settings = self.module.load_deployment_settings(secret_reader=synthetic_secret_reader)
        self.assertEqual(settings.internal_token, INTERNAL_TOKEN)

    def test_every_required_environment_setting_fails_closed_when_missing(self):
        for key in configuration_environment():
            values = configuration_environment()
            del values[key]
            with self.subTest(key=key), self.assertRaises(self.error):
                self.module.load_deployment_settings(values, secret_reader=synthetic_secret_reader)

    def test_loader_parses_every_supported_policy_without_coercing_secret_values(self):
        settings = self.load(
            TAIP_API_USERNAME="team", TAIP_CORE_URL="https://core:8443", TAIP_WEB_URL="http://web:8080",
            TAIP_REDIS_HOST="localhost", TAIP_REDIS_PORT="6380", TAIP_REDIS_USERNAME="team",
            TAIP_REDIS_NAMESPACE="taip:controlled", TAIP_REDIS_TLS="true",
            TAIP_MAX_BODY_BYTES="2048", TAIP_MAX_RESPONSE_BYTES="4096",
            TAIP_BODY_TIMEOUT_SECONDS="3.5", TAIP_REQUEST_TIMEOUT_SECONDS="12",
            TAIP_REDIS_LEASE_SECONDS="30", TAIP_GATEWAY_WORKERS="3",
            TAIP_HTTP_REQUESTS_PER_MINUTE="20", TAIP_HTTP_REQUESTS_PER_DAY="200",
            TAIP_HTTP_MAX_CONCURRENT="4", TAIP_LIVE_AI="true", TAIP_TLS_TERMINATED_BY_PROXY="true",
        )
        self.assertEqual(settings.basic_username, "team")
        self.assertEqual(settings.core_url, "https://core:8443")
        self.assertEqual(settings.web_url, "http://web:8080")
        self.assertEqual((settings.redis.host, settings.redis.port, settings.redis.username),
                         ("localhost", 6380, "team"))
        self.assertEqual(settings.redis.namespace, "taip:controlled")
        self.assertIs(settings.redis.tls, True)
        self.assertEqual((settings.max_body_bytes, settings.max_response_bytes), (2048, 4096))
        self.assertEqual((settings.body_timeout_seconds, settings.request_timeout_seconds,
                          settings.redis_lease_seconds), (3.5, 12, 30))
        self.assertEqual((settings.gateway_workers, settings.http_requests_per_minute,
                          settings.http_requests_per_day, settings.http_max_concurrent), (3, 20, 200, 4))
        self.assertIs(settings.live_ai, True)
        self.assertIs(settings.trust_tls_proxy, True)

    def test_environment_boolean_and_numeric_syntax_are_strict(self):
        for key in ("TAIP_LIVE_AI", "TAIP_REDIS_TLS", "TAIP_TLS_TERMINATED_BY_PROXY"):
            for value in ("True", "FALSE", "1", "yes", " true", "false ", "", True):
                with self.subTest(key=key, value=value), self.assertRaises(self.error):
                    self.load(**{key: value})
        for key in ("TAIP_GATEWAY_WORKERS", "TAIP_REQUEST_TIMEOUT_SECONDS"):
            for value in (" 2", "2 ", "NaN", "inf", "", True, None):
                with self.subTest(key=key, value=value), self.assertRaises(self.error):
                    self.load(**{key: value})
        for value in ("2.0", "+2", "2e0"):
            with self.subTest(value=value), self.assertRaises(self.error):
                self.load(TAIP_GATEWAY_WORKERS=value)

    def test_secret_reader_strips_terminal_newlines_only(self):
        def reader(path):
            return synthetic_secret_reader(path) + "\r\n"

        settings = self.module.load_deployment_settings(configuration_environment(), secret_reader=reader)
        self.assertEqual(settings.internal_token, INTERNAL_TOKEN)
        for suffix in (" ", "\t", "\x00"):
            def invalid_reader(path):
                return synthetic_secret_reader(path) + suffix
            with self.subTest(suffix=suffix), self.assertRaises(self.error):
                self.module.load_deployment_settings(configuration_environment(), secret_reader=invalid_reader)

    def test_reader_failures_and_invalid_digests_have_safe_errors(self):
        for failure in (OSError("PRIVATE-reader-path"), UnicodeError("PRIVATE-reader-data")):
            def reader(path):
                raise failure
            with self.subTest(failure=type(failure)), self.assertRaises(self.error) as caught:
                self.module.load_deployment_settings(configuration_environment(), secret_reader=reader)
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertNotIn("PRIVATE", repr(vars(caught.exception)))
            self.assertIsNone(caught.exception.__cause__)
        for value in ("x" * 64, "a" * 63, "a" * 65, b"a" * 64, " " + DIGEST.hex()):
            def reader(path):
                return value if path == "synthetic-public-digest" else synthetic_secret_reader(path)
            with self.subTest(kind=type(value)), self.assertRaises(self.error):
                self.module.load_deployment_settings(configuration_environment(), secret_reader=reader)

    def test_default_secret_reader_checks_regular_files_size_and_utf8(self):
        with tempfile.TemporaryDirectory(prefix="taip-controlled-secrets-") as directory:
            root = Path(directory)
            paths = {"TAIP_ACCESS_TOKEN_SHA256_FILE": root / "digest",
                     "TAIP_INTERNAL_TOKEN_FILE": root / "internal",
                     "TAIP_REDIS_PASSWORD_FILE": root / "redis"}
            paths["TAIP_ACCESS_TOKEN_SHA256_FILE"].write_text(DIGEST.hex() + "\n", encoding="utf8")
            paths["TAIP_INTERNAL_TOKEN_FILE"].write_text(INTERNAL_TOKEN + "\r\n", encoding="utf8")
            paths["TAIP_REDIS_PASSWORD_FILE"].write_text(REDIS_PASSWORD, encoding="utf8")
            environment = configuration_environment(**{key: str(path) for key, path in paths.items()})
            self.assertEqual(self.module.load_deployment_settings(environment).access_token_digest, DIGEST)
            for contents in (b"a" * 4097, b"\xff"):
                paths["TAIP_INTERNAL_TOKEN_FILE"].write_bytes(contents)
                with self.subTest(contents_length=len(contents)), self.assertRaises(self.error) as caught:
                    self.module.load_deployment_settings(environment)
                self.assertNotIn(str(root), str(caught.exception))
            for invalid_path in (root, root / "missing"):
                with self.subTest(kind=invalid_path.name), self.assertRaises(self.error):
                    self.module.load_deployment_settings({**environment, "TAIP_INTERNAL_TOKEN_FILE": str(invalid_path)})


if __name__ == "__main__":
    unittest.main()
