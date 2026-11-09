"""Explicit provider secret files, with no ambient or plaintext fallback."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from taip.ai_integration.config import load_provider_configs
from taip.ai_integration.errors import AIConfigurationError
from tests.ai_integration.test_config import provider_environment


class SecretFileTests(unittest.TestCase):
    def test_explicit_file_keys_load_without_mutating_environment(self):
        with TemporaryDirectory() as directory:
            environment = provider_environment()
            for provider in ("GEMINI", "GROQ", "OPENROUTER"):
                key = environment.pop(provider + "_API_KEY")
                file = Path(directory) / provider
                file.write_text(key + "\n", encoding="utf-8")
                environment[provider + "_API_KEY_FILE"] = str(file)
            before = environment.copy()
            configs = load_provider_configs(environment)
            self.assertEqual(configs[0].api_key, "fake-gemini-secret")
            self.assertEqual(environment, before)

    def test_ambiguous_inline_and_file_keys_fail_closed_without_echo(self):
        with self.assertRaises(AIConfigurationError) as caught:
            load_provider_configs(provider_environment() | {"GEMINI_API_KEY_FILE": "private-secret-path"})
        self.assertNotIn("private-secret-path", str(caught.exception))

    def test_missing_directory_large_and_invalid_utf8_files_are_safe_errors(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            large, invalid = base / "large", base / "invalid"
            large.write_bytes(b"x" * 4097)
            invalid.write_bytes(b"\xff")
            for path in (base, base / "missing", large, invalid):
                environment = provider_environment()
                del environment["GEMINI_API_KEY"]
                environment["GEMINI_API_KEY_FILE"] = str(path)
                with self.assertRaises(AIConfigurationError) as caught:
                    load_provider_configs(environment)
                self.assertNotIn(str(path), str(caught.exception))
