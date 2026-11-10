"""Host-path rendering and safe bootstrap reuse, using synthetic private files."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from taip.deployment.setup_public import prepare_deployment


class LauncherSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "container private"

    def snapshot(self):
        return {
            str(path.relative_to(self.directory)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.directory.rglob("*") if path.is_file()
        }

    def test_windows_host_paths_are_rendered_while_files_stay_in_mounted_directory(self):
        config = prepare_deployment(
            self.directory, environment_directory="C:\\Users\\Sorin O'Neil\\Școală,$\\launcher",
        )
        self.assertEqual(config, self.directory / "public.env")
        self.assertIn(
            "TAIP_INTERNAL_TOKEN_FILE='C:/Users/Sorin O\\'Neil/Școală,$/launcher/internal-token.txt'\n",
            config.read_text(encoding="utf-8"),
        )
        self.assertTrue((self.directory / "internal-token.txt").is_file())

    def test_reuse_without_existing_state_creates_complete_deployment(self):
        config = prepare_deployment(self.directory, reuse_existing=True)
        self.assertTrue(config.is_file())
        self.assertTrue((self.directory / "caddy-data").is_dir())
        self.assertEqual(len((self.directory / "access-token.txt").read_text().strip()), 64)

    def test_unchanged_reuse_preserves_credentials_data_and_file_timestamps(self):
        prepare_deployment(self.directory, environment_directory="C:/Private/launcher")
        (self.directory / "redis-data" / "persistent.rdb").write_bytes(b"redis state")
        before = self.snapshot()
        result = prepare_deployment(
            self.directory, environment_directory="C:/Private/launcher", reuse_existing=True,
        )
        self.assertEqual(result, self.directory / "public.env")
        self.assertEqual(self.snapshot(), before)

    def test_explicit_new_host_root_updates_only_environment_paths(self):
        prepare_deployment(self.directory, environment_directory="C:/Old/launcher")
        before = self.snapshot()
        config = prepare_deployment(
            self.directory, environment_directory="D:/New/launcher", reuse_existing=True,
        )
        self.assertIn("TAIP_REDIS_DATA_DIR='D:/New/launcher/redis-data'\n", config.read_text())
        after = self.snapshot()
        self.assertEqual(
            {name: entry for name, entry in after.items() if name != "public.env"},
            {name: entry for name, entry in before.items() if name != "public.env"},
        )
        stable = self.snapshot()
        prepare_deployment(self.directory, environment_directory="D:/New/launcher", reuse_existing=True)
        self.assertEqual(self.snapshot(), stable)

    def test_equivalent_environment_order_is_preserved_on_reuse(self):
        config = prepare_deployment(self.directory, environment_directory="C:/Private/launcher")
        config.write_text("\n".join(reversed(config.read_text().splitlines())) + "\n")
        before = self.snapshot()
        prepare_deployment(self.directory, environment_directory="C:/Private/launcher", reuse_existing=True)
        self.assertEqual(self.snapshot(), before)

    def test_host_root_relocation_preserves_existing_container_ownership(self):
        config = prepare_deployment(self.directory, environment_directory="C:/Private/launcher")
        lines = config.read_text().splitlines()
        lines = [
            "TAIP_APP_UID=1240" if line.startswith("TAIP_APP_UID=") else
            "TAIP_APP_GID=1241" if line.startswith("TAIP_APP_GID=") else line
            for line in lines
        ]
        config.write_text("\n".join(lines) + "\n")
        prepare_deployment(self.directory, environment_directory="D:/Private/launcher", reuse_existing=True)
        self.assertIn("TAIP_APP_UID=1240\n", config.read_text())
        self.assertIn("TAIP_APP_GID=1241\n", config.read_text())

    def test_host_path_override_keeps_default_never_overwrite_contract(self):
        prepare_deployment(self.directory)
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            prepare_deployment(self.directory, environment_directory="C:/Private/launcher")
        self.assertEqual(self.snapshot(), before)

    def test_reuse_rejects_partial_state_without_creating_or_rotating_files(self):
        self.directory.mkdir()
        (self.directory / "access-token.txt").write_text("operator-owned partial state")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "existing deployment"):
            prepare_deployment(self.directory, reuse_existing=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_reuse_rejects_corrupt_credentials_hash_acl_and_paths_without_changes(self):
        mutations = {
            "access-token.txt": "bad secret must never appear in diagnostics\n",
            "access-token.sha256": "0" * 64 + "\n",
            "internal-token.txt": "x" * 63 + "\n",
            "redis-password.txt": "x" * 64 + "\n",
            "redis.acl": "user default on nopass +@all\n",
            "public.env": "TAIP_DOMAIN=localhost\n",
        }
        for index, (name, content) in enumerate(mutations.items()):
            with self.subTest(name=name):
                self.directory = Path(self.temporary.name) / str(index)
                prepare_deployment(self.directory)
                (self.directory / name).write_text(content)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "existing deployment") as raised:
                    prepare_deployment(self.directory, reuse_existing=True)
                self.assertNotIn(content.strip(), str(raised.exception))
                self.assertEqual(self.snapshot(), before)

    def test_reuse_rejects_directory_replaced_by_file_without_changes(self):
        prepare_deployment(self.directory)
        (self.directory / "caddy-data").rmdir()
        (self.directory / "caddy-data").write_text("preserve unexpected file")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "caddy-data"):
            prepare_deployment(self.directory, reuse_existing=True)
        self.assertEqual(self.snapshot(), before)

    def test_reuse_rejects_requested_parameter_changes_without_changes(self):
        prepare_deployment(self.directory)
        before = self.snapshot()
        for arguments in (
            {"domain": "example.com"}, {"http_port": 8081}, {"https_port": 8444},
            {"workers": 3}, {"bind_address": "0.0.0.0"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, "existing deployment"):
                    prepare_deployment(self.directory, reuse_existing=True, **arguments)
                self.assertEqual(self.snapshot(), before)

    def test_reuse_rejects_duplicate_extra_or_inconsistent_environment_fields(self):
        for index, extra in enumerate((
            "TAIP_DOMAIN=localhost\n", "UNEXPECTED=value\n",
            "TAIP_PUBLIC_ORIGIN=https://untrusted.example\n",
        )):
            with self.subTest(extra=extra):
                self.directory = Path(self.temporary.name) / str(index)
                config = prepare_deployment(self.directory)
                config.write_text(config.read_text() + extra)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "existing deployment"):
                    prepare_deployment(self.directory, reuse_existing=True)
                self.assertEqual(self.snapshot(), before)

    def test_reuse_rejects_inconsistent_existing_path_even_with_new_explicit_root(self):
        config = prepare_deployment(self.directory, environment_directory="C:/Private/launcher")
        config.write_text(config.read_text().replace(
            "C:/Private/launcher/redis-data", "C:/Unexpected/redis-data",
        ))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "existing deployment"):
            prepare_deployment(self.directory, environment_directory="D:/New/launcher", reuse_existing=True)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_host_roots_fail_before_writing_any_files(self):
        for value in ("relative/path", "C:relative", "", "C:/unsafe\nROOT=x", "C:/unsafe\x00x", "C:/unsafe\tx"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    prepare_deployment(self.directory, environment_directory=value)
                self.assertFalse(self.directory.exists())

    def test_cli_accepts_reusable_host_path_without_printing_credentials(self):
        arguments = [
            sys.executable, "-m", "taip.deployment.setup_public", "--directory", str(self.directory),
            "--environment-directory", "C:/Private/launcher", "--reuse-existing",
        ]
        first = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.snapshot()
        second = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.snapshot(), before)
        for name in ("access-token.txt", "internal-token.txt", "redis-password.txt"):
            self.assertNotIn((self.directory / name).read_text().strip(), first.stdout + first.stderr + second.stdout + second.stderr)
        self.assertIn(str(self.directory / "public.env"), first.stdout)


if __name__ == "__main__":
    unittest.main()
