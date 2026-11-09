"""Controlled configuration checks; no real environment or credentials are read."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from importlib import import_module, util
import os
import sys
import unittest
from unittest.mock import patch


def provider_environment():
    return {
        "GEMINI_API_KEY": "fake-gemini-secret",
        "GEMINI_MODEL": "gemini/vendor-model-v1",
        "GROQ_API_KEY": "fake-groq-secret",
        "GROQ_MODEL": "vendor/exact-model:free",
        "OPENROUTER_API_KEY": "fake-openrouter-secret",
        "OPENROUTER_MODEL": "vendor/another-model-v2",
    }


class UnreadableEnvironment(Mapping):
    def __getitem__(self, key):
        raise AssertionError("Import must not inspect the environment")

    def __iter__(self):
        raise AssertionError("Import must not inspect the environment")

    def __len__(self):
        raise AssertionError("Import must not inspect the environment")


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.config"
        self.assertIsNotNone(util.find_spec(name), "The provider configuration module must exist")
        self.config = import_module(name)
        self.error = import_module("taip.ai_integration.errors").AIConfigurationError

    def make_config(self, **changes):
        values = {
            "model_id": "gemini",
            "provider": self.config.ProviderKind.GEMINI,
            "api_model_id": "vendor/exact-model:free",
            "api_key": "fake-never-print-this-secret",
        }
        values.update(changes)
        return self.config.ProviderConfig(**values)

    def test_provider_configuration_preserves_identity_and_safe_defaults(self):
        config = self.make_config()
        self.assertEqual(config.model_id, "gemini")
        self.assertEqual(config.provider.value, "gemini")
        self.assertEqual(config.api_model_id, "vendor/exact-model:free")
        self.assertEqual(config.api_key, "fake-never-print-this-secret")
        self.assertEqual(config.timeout_seconds, 60.0)
        self.assertEqual(config.max_output_tokens, 8192)
        self.assertEqual(config.temperature, 0.0)
        self.assertEqual(config.max_response_bytes, 1048576)
        self.assertIs(config.use_json_schema, False)
        self.assertEqual(self.config.ProviderKind.GROQ.value, "groq")
        self.assertEqual(self.config.ProviderKind.OPENROUTER.value, "openrouter")

    def test_config_is_immutable_and_has_no_instance_dictionary(self):
        config = self.make_config()
        with self.assertRaises(FrozenInstanceError):
            config.api_model_id = "another-model"
        self.assertFalse(hasattr(config, "__dict__"))

    def test_api_key_is_excluded_from_repr_and_error_messages(self):
        secret = "fake-never-print-this-secret"
        self.assertNotIn(secret, repr(self.make_config(api_key=secret)))
        with self.assertRaises(self.error) as caught:
            self.make_config(api_key=" " + secret)
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.code, "configuration_error")
        self.assertEqual(error.field, "api_key")
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        self.assertNotIn(secret, repr(vars(error)))

    def test_strings_require_nonempty_trimmed_values_without_coercion(self):
        for field in ("model_id", "api_model_id", "api_key"):
            for value in ("", " ", "\t", " leading", "trailing ", None, 42, True):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(self.error) as caught:
                        self.make_config(**{field: value})
                    self.assertEqual(caught.exception.field, field)

    def test_provider_requires_provider_kind_instead_of_string_or_other_values(self):
        for value in ("gemini", "unknown", None, True, 1):
            with self.subTest(value=value):
                with self.assertRaises(self.error) as caught:
                    self.make_config(provider=value)
                self.assertEqual(caught.exception.field, "provider")

    def test_timeout_requires_positive_finite_number_and_rejects_boolean(self):
        for value in (0, -1, float("nan"), float("inf"), -float("inf"), True, False, "60", None):
            with self.subTest(value=value):
                with self.assertRaises(self.error) as caught:
                    self.make_config(timeout_seconds=value)
                self.assertEqual(caught.exception.field, "timeout_seconds")
        self.assertEqual(self.make_config(timeout_seconds=1).timeout_seconds, 1)
        self.assertEqual(self.make_config(timeout_seconds=0.01).timeout_seconds, 0.01)

    def test_temperature_accepts_endpoints_and_rejects_nonfinite_or_outside_bounds(self):
        for value in (-0.01, 2.01, float("nan"), float("inf"), -float("inf"), True, "0", None):
            with self.subTest(value=value):
                with self.assertRaises(self.error) as caught:
                    self.make_config(temperature=value)
                self.assertEqual(caught.exception.field, "temperature")
        self.assertEqual(self.make_config(temperature=0).temperature, 0)
        self.assertEqual(self.make_config(temperature=2).temperature, 2)

    def test_limits_require_positive_integers_without_lossy_coercion(self):
        for field in ("max_output_tokens", "max_response_bytes"):
            for value in (0, -1, 1.0, 1.5, True, False, "8192", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(self.error) as caught:
                        self.make_config(**{field: value})
                    self.assertEqual(caught.exception.field, field)
            self.assertEqual(getattr(self.make_config(**{field: 1}), field), 1)

    def test_schema_flag_requires_exact_boolean(self):
        for value in (0, 1, "true", "false", None):
            with self.subTest(value=value):
                with self.assertRaises(self.error) as caught:
                    self.make_config(use_json_schema=value)
                self.assertEqual(caught.exception.field, "use_json_schema")
        self.assertIs(self.make_config(use_json_schema=True).use_json_schema, True)

    def test_loader_requires_every_provider_and_preserves_exact_vendor_model_slugs(self):
        environment = provider_environment()
        before = dict(environment)
        configs = self.config.load_provider_configs(environment)
        self.assertIsInstance(configs, tuple)
        self.assertEqual(tuple(config.model_id for config in configs), ("gemini", "groq", "openrouter"))
        self.assertEqual(tuple(config.provider for config in configs), (
            self.config.ProviderKind.GEMINI,
            self.config.ProviderKind.GROQ,
            self.config.ProviderKind.OPENROUTER,
        ))
        self.assertEqual(tuple(config.api_model_id for config in configs), (
            "gemini/vendor-model-v1", "vendor/exact-model:free", "vendor/another-model-v2",
        ))
        self.assertEqual(tuple(config.api_key for config in configs), (
            "fake-gemini-secret", "fake-groq-secret", "fake-openrouter-secret",
        ))
        self.assertTrue(all(config.use_json_schema is False for config in configs))
        self.assertEqual(environment, before)

    def test_loader_reports_missing_or_invalid_required_variables_safely(self):
        for field in provider_environment():
            for value in (None, "", " ", " untrimmed ", True, 42):
                with self.subTest(field=field, value=value):
                    environment = provider_environment()
                    if value is None:
                        del environment[field]
                    else:
                        environment[field] = value
                    with self.assertRaises(self.error) as caught:
                        self.config.load_provider_configs(environment)
                    self.assertEqual(caught.exception.field, field)
                    for secret in ("fake-gemini-secret", "fake-groq-secret", "fake-openrouter-secret"):
                        self.assertNotIn(secret, str(caught.exception))
                        self.assertNotIn(secret, repr(caught.exception))
        with self.assertRaises(self.error):
            self.config.load_provider_configs({})

    def test_loader_applies_shared_controls_and_independent_schema_flags(self):
        environment = provider_environment() | {
            "TAIP_AI_TIMEOUT_SECONDS": "12.5",
            "TAIP_AI_MAX_OUTPUT_TOKENS": "2048",
            "TAIP_AI_TEMPERATURE": "0.25",
            "TAIP_AI_MAX_RESPONSE_BYTES": "4096",
            "GEMINI_JSON_SCHEMA": "true",
            "GROQ_JSON_SCHEMA": "false",
            "OPENROUTER_JSON_SCHEMA": "true",
            "UNRELATED_PROVIDER_API_KEY": "ignored-fake-secret",
        }
        before = dict(environment)
        configs = self.config.load_provider_configs(environment)
        for config in configs:
            self.assertEqual(config.timeout_seconds, 12.5)
            self.assertEqual(config.max_output_tokens, 2048)
            self.assertEqual(config.temperature, 0.25)
            self.assertEqual(config.max_response_bytes, 4096)
        self.assertEqual(tuple(config.use_json_schema for config in configs), (True, False, True))
        self.assertEqual(environment, before)

    def test_loader_rejects_invalid_numeric_controls_without_exposing_values(self):
        invalid = {
            "TAIP_AI_TIMEOUT_SECONDS": ("", "0", "-1", "nan", "inf", "-inf", "true", "fake-secret"),
            "TAIP_AI_MAX_OUTPUT_TOKENS": ("", "0", "-1", "1.5", "true", "nan", "fake-secret"),
            "TAIP_AI_TEMPERATURE": ("", "-0.1", "2.1", "nan", "inf", "true", "fake-secret"),
            "TAIP_AI_MAX_RESPONSE_BYTES": ("", "0", "-1", "1.5", "true", "inf", "fake-secret"),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(self.error) as caught:
                        self.config.load_provider_configs(provider_environment() | {field: value})
                    self.assertEqual(caught.exception.field, field)
                    self.assertNotIn("fake-secret", str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)

    def test_loader_schema_flags_accept_only_lowercase_true_or_false(self):
        for field in ("GEMINI_JSON_SCHEMA", "GROQ_JSON_SCHEMA", "OPENROUTER_JSON_SCHEMA"):
            for value in ("TRUE", "False", " true", "false ", "1", "0", "", True, False):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(self.error) as caught:
                        self.config.load_provider_configs(provider_environment() | {field: value})
                    self.assertEqual(caught.exception.field, field)

    def test_loader_reads_environment_at_call_time_and_does_not_merge_explicit_mapping(self):
        with patch.object(os, "environ", provider_environment()):
            first = self.config.load_provider_configs()
        with patch.object(os, "environ", provider_environment() | {"GEMINI_MODEL": "changed-at-call-time"}):
            second = self.config.load_provider_configs()
            with self.assertRaises(self.error):
                self.config.load_provider_configs({})
        self.assertEqual(first[0].api_model_id, "gemini/vendor-model-v1")
        self.assertEqual(second[0].api_model_id, "changed-at-call-time")

    def test_import_does_not_read_environment(self):
        name = "taip.ai_integration._config_import_test"
        spec = util.spec_from_file_location(name, self.config.__file__)
        module = util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: module}):
            with patch.object(os, "environ", UnreadableEnvironment()):
                spec.loader.exec_module(module)


if __name__ == "__main__":
    unittest.main()
