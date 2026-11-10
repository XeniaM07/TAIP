"""Windows key parsing uses only synthetic files inside disposable directories."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "deployment" / "windows" / "keys.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
FIELDS = (
    "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
    "GEMINI_MODEL", "GROQ_MODEL", "OPENROUTER_MODEL",
)
VALUES = {
    "GEMINI_API_KEY": "synthetic-gemini-key",
    "GROQ_API_KEY": "synthetic-groq-key",
    "OPENROUTER_API_KEY": "synthetic-openrouter-key",
    "GEMINI_MODEL": "vendor/gemini-exact",
    "GROQ_MODEL": "vendor/groq-exact",
    "OPENROUTER_MODEL": "vendor/openrouter-exact:free",
}
DEFAULT_MODELS = {
    "GEMINI_MODEL": "gemini-3.5-flash-lite",
    "GROQ_MODEL": "openai/gpt-oss-120b",
    "OPENROUTER_MODEL": "qwen/qwen3.7-flash",
}
KEY_VALUES = {name: value for name, value in VALUES.items() if name.endswith("_API_KEY")}


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is unavailable")
class WindowsKeyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "The Windows key module must exist")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.desktop = self.base / "OneDrive desktop space ' unicode-\u0219"
        self.desktop.mkdir()
        self.private = self.base / "private space ' unicode-\u0219 $literal"
        self.keys = self.desktop / "keys"

    def write_keys(self, values=None, *, prefix="", target=None, encoding="utf-8"):
        values = VALUES if values is None else values
        (target or self.keys).write_text(
            prefix + "".join(f"{name}={value}\n" for name, value in values.items()),
            encoding=encoding,
        )

    def run_module(self, script=None):
        if script is None:
            script = "Get-TaipKeyConfiguration -DesktopDirectory $env:TAIP_TEST_DESKTOP"
        runner = self.base / "exercise.ps1"
        runner.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "try {\n. $env:TAIP_TEST_MODULE\n$result = & {\n"
            + script
            + "\n}\nConvertTo-Json -InputObject $result -Depth 8 -Compress\n"
            "} catch { ConvertTo-Json -InputObject @{Error = $_.Exception.Message} -Compress }\n",
            encoding="utf-8-sig",
        )
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
            env={
                **os.environ,
                "TAIP_TEST_MODULE": str(MODULE),
                "TAIP_TEST_DESKTOP": str(self.desktop),
                "TAIP_TEST_PRIVATE": str(self.private),
                "TAIP_TEST_SENTINEL": str(self.base / "must-not-exist"),
            },
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout.lstrip("\ufeff"))

    def assert_safe_error(self, result, *, field=None, forbidden=()):
        self.assertIn("Error", result)
        if field:
            self.assertIn(field, result["Error"])
        for value in forbidden:
            self.assertNotIn(value, result["Error"])

    def test_missing_configuration_reports_expected_literal_template_path(self):
        result = self.run_module()
        self.assertEqual(result["Mode"], "missing")
        self.assertEqual(result["Path"], str(self.keys))
        self.assertEqual(result["Values"], {})
        self.assertRegex(result["Fingerprint"], r"^[0-9a-f]{64}$")

    def test_exact_keys_precedes_keys_txt_and_fallback_is_supported(self):
        self.write_keys(target=self.desktop / "keys.txt")
        fallback = self.run_module()
        self.assertEqual(fallback["Mode"], "live")
        self.assertEqual(fallback["Path"], str(self.desktop / "keys.txt"))
        self.write_keys({}, prefix="MODE=mock\n")
        exact = self.run_module()
        self.assertEqual(exact["Mode"], "mock")
        self.assertEqual(exact["Path"], str(self.keys))

    def test_all_six_complete_fields_select_live_without_mode(self):
        self.write_keys()
        result = self.run_module()
        self.assertEqual(result["Mode"], "live")
        self.assertEqual(result["Values"], VALUES)

    def test_three_api_keys_select_live_with_exact_project_model_defaults(self):
        self.write_keys(KEY_VALUES)
        result = self.run_module()
        self.assertEqual(result.get("Mode"), "live", result)
        self.assertEqual(result["Values"], {**KEY_VALUES, **DEFAULT_MODELS})

    def test_each_explicit_model_override_preserves_other_project_defaults(self):
        for field in DEFAULT_MODELS:
            with self.subTest(field=field):
                self.write_keys({**KEY_VALUES, field: "vendor/custom-exact-id"})
                result = self.run_module()
                self.assertEqual(result.get("Mode"), "live", result)
                self.assertEqual(result["Values"], {**KEY_VALUES, **DEFAULT_MODELS, field: "vendor/custom-exact-id"})

    def test_blank_models_use_project_defaults_for_complete_api_keys(self):
        self.write_keys({**KEY_VALUES, **dict.fromkeys(DEFAULT_MODELS, "")})
        result = self.run_module()
        self.assertEqual(result.get("Mode"), "live", result)
        self.assertEqual(result["Values"], {**KEY_VALUES, **DEFAULT_MODELS})

    def test_one_or_two_api_keys_reject_incomplete_credentials_with_safe_field_names(self):
        for supplied in (
            {"GEMINI_API_KEY": "synthetic-one-key"},
            {"GEMINI_API_KEY": "synthetic-one-key", "GROQ_API_KEY": "synthetic-two-key"},
        ):
            with self.subTest(supplied=tuple(supplied)):
                self.write_keys({**supplied, **DEFAULT_MODELS})
                result = self.run_module()
                self.assert_safe_error(result, field="OPENROUTER_API_KEY", forbidden=tuple(supplied.values()))
                self.assertNotIn("GEMINI_MODEL", result["Error"])

    def test_models_without_api_credentials_remain_mock(self):
        self.write_keys(DEFAULT_MODELS)
        result = self.run_module()
        self.assertEqual(result.get("Mode"), "mock", result)

    def test_implicit_and_explicit_project_defaults_have_the_same_fingerprint(self):
        self.write_keys(KEY_VALUES)
        implicit = self.run_module()
        self.assertEqual(implicit.get("Mode"), "live")
        self.write_keys({**KEY_VALUES, **DEFAULT_MODELS})
        self.assertEqual(self.run_module()["Fingerprint"], implicit["Fingerprint"])

    def test_blank_and_comment_only_configurations_select_mock(self):
        for contents in ("", "# comment\n\n  # indented comment\n", "".join(f"{name}=\n" for name in FIELDS)):
            with self.subTest(contents=contents):
                self.keys.write_text(contents, encoding="utf-8")
                self.assertEqual(self.run_module()["Mode"], "mock")

    def test_explicit_mock_allows_incomplete_values_without_creating_credentials(self):
        self.write_keys({"GEMINI_API_KEY": "synthetic-partial"}, prefix="MODE=mock\n")
        result = self.run_module(
            "$config = Get-TaipKeyConfiguration -DesktopDirectory $env:TAIP_TEST_DESKTOP\n"
            "$path = Write-TaipProviderConfiguration -Configuration $config -PrivateDirectory $env:TAIP_TEST_PRIVATE\n"
            "@{Mode=$config.Mode; Path=$path; Created=[IO.Directory]::Exists($env:TAIP_TEST_PRIVATE)}"
        )
        self.assertEqual(result, {"Mode": "mock", "Path": "", "Created": False})

    def test_partial_or_explicit_live_blank_configuration_lists_safe_missing_fields(self):
        for prefix, values in (("", {"GEMINI_API_KEY": "synthetic-sensitive-partial"}), ("MODE=live\n", {})):
            with self.subTest(prefix=prefix):
                self.write_keys(values, prefix=prefix)
                self.assert_safe_error(self.run_module(), field="GROQ_API_KEY", forbidden=("synthetic-sensitive-partial",))

    def test_invalid_mode_and_duplicate_fields_never_echo_values(self):
        cases = (
            ("MODE=synthetic-bad-mode\n", "MODE", "synthetic-bad-mode"),
            ("GEMINI_API_KEY=synthetic-first\nGEMINI_API_KEY=synthetic-second\n", "GEMINI_API_KEY", "synthetic-second"),
            ("MODE=mock\nMODE=live\n", "MODE", "MODE=live"),
        )
        for contents, field, forbidden in cases:
            with self.subTest(field=field, contents=contents):
                self.keys.write_text(contents, encoding="utf-8")
                self.assert_safe_error(self.run_module(), field=field, forbidden=(forbidden,))

    def test_unknown_fields_or_malformed_lines_do_not_echo_any_line_content(self):
        for contents in ("SYNTHETIC_SECRET_NAME=synthetic-token\n", "synthetic-token-without-equals\n"):
            with self.subTest(contents=contents):
                self.keys.write_text(contents, encoding="utf-8")
                self.assert_safe_error(self.run_module(), forbidden=("synthetic-token", "SYNTHETIC_SECRET_NAME"))

    def test_bom_comments_whitespace_quotes_and_equals_are_parsed_literally(self):
        values = {**VALUES, "GEMINI_API_KEY": "abc=def==", "GROQ_API_KEY": "$env:NOT_EXPANDED"}
        self.keys.write_text(
            "\ufeff# UTF8 BOM\r\n\r\n" + "\r\n".join(
                f"  {name} = {chr(34)}{value}{chr(34)}  " if name.endswith("_KEY") else f"{name}='{value}'"
                for name, value in values.items()
            ), encoding="utf-8",
        )
        self.assertEqual(self.run_module()["Values"], values)

    def test_powershell_subexpressions_are_data_and_never_executed(self):
        token = "$(Set-Content -LiteralPath $env:TAIP_TEST_SENTINEL -Value executed)"
        self.write_keys({**VALUES, "GEMINI_API_KEY": token})
        self.assertEqual(self.run_module()["Values"]["GEMINI_API_KEY"], token)
        self.assertFalse((self.base / "must-not-exist").exists())

    def test_control_characters_invalid_utf8_and_large_files_fail_safely(self):
        for data in (
            b"MODE=mock\nGEMINI_API_KEY=synthetic-secret\x00tail\n",
            b"MODE=mock\nGEMINI_API_KEY=synthetic-secret\ttail\n",
            b"MODE=mock\nGEMINI_API_KEY=synthetic-secret\x7ftail\n",
            b"# synthetic-secret\xff\n",
            b"#" + b"x" * 32768,
        ):
            with self.subTest(length=len(data)):
                self.keys.write_bytes(data)
                self.assert_safe_error(self.run_module(), forbidden=("synthetic-secret",))

    def test_key_and_model_utf8_byte_limits_are_enforced_including_exact_boundaries(self):
        for field, valid, invalid in (
            ("GEMINI_API_KEY", "k" * 4096, "k" * 4097),
            ("GROQ_MODEL", "m" * 512, "m" * 513),
            ("OPENROUTER_MODEL", "\u0219" * 256, "\u0219" * 257),
        ):
            with self.subTest(field=field):
                self.write_keys({**VALUES, field: valid})
                self.assertEqual(self.run_module()["Mode"], "live")
                self.write_keys({**VALUES, field: invalid})
                self.assert_safe_error(self.run_module(), field=field, forbidden=(invalid,))

    def test_fingerprint_is_stable_across_order_comments_bom_and_explicit_live(self):
        self.write_keys()
        first = self.run_module()["Fingerprint"]
        self.write_keys(dict(reversed(tuple(VALUES.items()))), prefix="# formatting only\nMODE=live\n", encoding="utf-8-sig")
        self.assertEqual(self.run_module()["Fingerprint"], first)

    def test_fingerprint_changes_for_key_rotation_model_or_effective_mode(self):
        self.write_keys()
        original = self.run_module()["Fingerprint"]
        for field in ("GEMINI_API_KEY", "GROQ_MODEL"):
            self.write_keys({**VALUES, field: "synthetic-rotated-value"})
            self.assertNotEqual(self.run_module()["Fingerprint"], original)
        self.write_keys(prefix="MODE=mock\n")
        self.assertNotEqual(self.run_module()["Fingerprint"], original)

    def test_template_is_utf8_without_bom_with_blank_fields_and_never_overwrites(self):
        script = "New-TaipKeyTemplate -Path (Join-Path $env:TAIP_TEST_DESKTOP 'keys')\n@{Written=$true}"
        self.run_module(script)
        data = self.keys.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        fields = dict(line.split("=", 1) for line in data.decode("utf-8").splitlines() if line and not line.startswith("#"))
        self.assertEqual(fields, dict.fromkeys(KEY_VALUES, ""))
        for field, model in DEFAULT_MODELS.items():
            self.assertIn(f"# {field}={model}", data.decode("utf-8"))
        self.assertEqual(self.run_module()["Mode"], "mock")
        self.write_keys(KEY_VALUES)
        before = self.keys.read_bytes()
        self.run_module(script)
        self.assertEqual(self.keys.read_bytes(), before)
        self.assertEqual(self.run_module()["Mode"], "live")

    def write_provider_files(self):
        return self.run_module(
            "$config = Get-TaipKeyConfiguration -DesktopDirectory $env:TAIP_TEST_DESKTOP\n"
            "$path = Write-TaipProviderConfiguration -Configuration $config -PrivateDirectory $env:TAIP_TEST_PRIVATE\n"
            "@{Path=$path}"
        )

    def test_live_writer_keeps_keys_out_of_env_and_uses_literal_unicode_apostrophe_paths(self):
        values = {**VALUES, "GROQ_MODEL": "vendor/model'quoted$literal"}
        self.write_keys(values)
        result = self.write_provider_files()
        env_file = self.private / "providers.env"
        self.assertEqual(result, {"Path": str(env_file)})
        data = env_file.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        lines = dict(line.split("=", 1) for line in data.decode("utf-8").splitlines() if line and not line.startswith("#"))
        self.assertEqual(len(lines), 6)
        for provider in ("GEMINI", "GROQ", "OPENROUTER"):
            key = values[provider + "_API_KEY"]
            self.assertNotIn(key, data.decode("utf-8"))
            path_text = lines[provider + "_API_KEY_FILE"]
            self.assertTrue(path_text.startswith("'") and path_text.endswith("'"))
            path = Path(path_text[1:-1].replace("\\'", "'"))
            self.assertEqual(path.parent, self.private / "provider-keys")
            self.assertEqual(path.read_bytes(), key.encode("utf-8"))
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            expected_model = "'" + values[provider + "_MODEL"].replace("'", "\\'") + "'"
            self.assertEqual(lines[provider + "_MODEL"], expected_model)
        self.assertFalse(any(path.is_relative_to(ROOT) for path in self.private.rglob("*")))

    def test_unchanged_provider_bytes_are_not_rewritten_and_rotation_updates_only_changed_key(self):
        self.write_keys()
        self.write_provider_files()
        files = tuple(path for path in self.private.rglob("*") if path.is_file())
        self.assertEqual(len(files), 4)
        for path in files:
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        self.write_provider_files()
        self.assertTrue(all(path.stat().st_mtime_ns == 1_000_000_000 for path in files))
        self.write_keys({**VALUES, "GEMINI_API_KEY": "synthetic-rotated-key"})
        self.write_provider_files()
        changed = [path for path in files if path.stat().st_mtime_ns != 1_000_000_000]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].read_text(encoding="utf-8"), "synthetic-rotated-key")


if __name__ == "__main__":
    unittest.main()
