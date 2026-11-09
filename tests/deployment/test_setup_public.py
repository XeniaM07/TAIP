"""Private bootstrap behavior; all generated credentials belong to temporary tests."""

from contextlib import redirect_stdout
from hashlib import sha256
from importlib import import_module, util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class PublicSetupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            util.find_spec("taip.deployment.setup_public"),
            "The private deployment bootstrap must exist",
        )
        self.module = import_module("taip.deployment.setup_public")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.directory = self.base / "private deployment"

    def test_generates_distinct_credentials_and_a_matching_hash_without_printing_secrets(self):
        output = io.StringIO()
        with redirect_stdout(output):
            config = self.module.prepare_deployment(self.directory)
        self.assertEqual(config, self.directory / "public.env")
        credentials = [
            (self.directory / name).read_text().strip()
            for name in ("access-token.txt", "internal-token.txt", "redis-password.txt")
        ]
        self.assertEqual(len(set(credentials)), 3)
        for credential in credentials:
            self.assertRegex(credential, r"^[A-Za-z0-9_-]{64}$")
            self.assertNotIn(credential, config.read_text())
            self.assertNotIn(credential, output.getvalue())
        self.assertEqual(
            (self.directory / "access-token.sha256").read_text().strip(),
            sha256(credentials[0].encode()).hexdigest(),
        )
        acl = (self.directory / "redis.acl").read_text()
        self.assertIn("user default off", acl)
        self.assertIn("#" + sha256(credentials[2].encode()).hexdigest(), acl)
        self.assertIn("~taip:*", acl)
        self.assertNotIn(credentials[2], acl)
        self.assertNotIn("+@all", acl)
        self.assertNotIn("+config", acl)
        self.assertNotIn("+acl", acl)
        self.assertEqual(output.getvalue(), "")

    def test_public_settings_and_private_paths_are_usable_without_secret_values(self):
        config = self.module.prepare_deployment(
            self.directory, domain="timetable.example.com", http_port=80,
            https_port=443, workers=4,
        )
        environment = dict(
            line.split("=", 1) for line in config.read_text().splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(environment["TAIP_PUBLIC_ORIGIN"], "https://timetable.example.com")
        self.assertEqual(environment["TAIP_DOMAIN"], "timetable.example.com")
        self.assertEqual(environment["TAIP_HTTP_PORT"], "80")
        self.assertEqual(environment["TAIP_HTTPS_PORT"], "443")
        self.assertEqual(environment["TAIP_GATEWAY_WORKERS"], "4")
        for key, name in {
            "TAIP_ACCESS_TOKEN_SHA256_FILE": "access-token.sha256",
            "TAIP_INTERNAL_TOKEN_FILE": "internal-token.txt",
            "TAIP_REDIS_PASSWORD_FILE": "redis-password.txt",
            "TAIP_REDIS_ACL_FILE": "redis.acl",
            "TAIP_REDIS_DATA_DIR": "redis-data",
            "TAIP_CADDY_DATA_DIR": "caddy-data",
            "TAIP_CADDY_CONFIG_DIR": "caddy-config",
        }.items():
            self.assertEqual(environment[key], "'" + (self.directory / name).as_posix() + "'")
        self.assertTrue((self.directory / "redis-data").is_dir())
        self.assertTrue((self.directory / "caddy-data").is_dir())
        self.assertTrue((self.directory / "caddy-config").is_dir())
        self.assertRegex(environment["TAIP_APP_UID"], r"^[0-9]+$")
        self.assertRegex(environment["TAIP_APP_GID"], r"^[0-9]+$")

    def test_default_origin_keeps_the_nonstandard_https_port(self):
        config = self.module.prepare_deployment(self.directory)
        self.assertIn("TAIP_PUBLIC_ORIGIN=https://localhost:8443\n", config.read_text())

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is needed to parse Compose")
    def test_compose_tmpfs_options_remain_with_each_absolute_mount(self):
        config = self.module.prepare_deployment(self.directory)
        compose = Path(__file__).resolve().parents[2] / "compose.public.yaml"
        rendered = subprocess.run(
            ["docker", "compose", "--env-file", str(config), "-f", str(compose),
             "config", "--format", "json"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        services = json.loads(rendered.stdout)["services"]
        for service, expected_count in (("core", 1), ("gateway", 1), ("redis", 1), ("web", 3), ("caddy", 1)):
            with self.subTest(service=service):
                mounts = services[service]["tmpfs"]
                self.assertEqual(len(mounts), expected_count)
                for mount in mounts:
                    target, separator, options = mount.partition(":")
                    self.assertTrue(target.startswith("/"), "tmpfs target must be absolute")
                    self.assertEqual(separator, ":")
                    expected_mode = "mode=1777" if target == "/tmp" else "mode=0700"
                    self.assertIn(expected_mode, options.split(","))

    def test_bind_address_defaults_to_loopback_and_accepts_only_explicit_ip_addresses(self):
        config = self.module.prepare_deployment(self.directory)
        self.assertIn("TAIP_BIND_ADDRESS=127.0.0.1\n", config.read_text())
        for index, address in enumerate(("0.0.0.0", "::1", "2001:db8::1")):
            with self.subTest(address=address):
                config = self.module.prepare_deployment(
                    self.base / f"explicit-bind-{index}", bind_address=address,
                )
                self.assertIn(f"TAIP_BIND_ADDRESS={address}\n", config.read_text())
        for address in ("", "localhost", "127.0.0.1:8080", "0.0.0.0\nANY=value", "fe80::1%eth0", 1234):
            with self.subTest(invalid_address=address):
                destination = self.base / "invalid-bind"
                with self.assertRaises(ValueError):
                    self.module.prepare_deployment(destination, bind_address=address)
                self.assertFalse(destination.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are not Windows ACLs")
    def test_private_files_and_directories_have_restrictive_modes(self):
        self.module.prepare_deployment(self.directory)
        for path in (self.directory, *self.directory.iterdir()):
            wanted = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), wanted)

    def test_rejects_invalid_domain_ports_and_workers_before_creating_files(self):
        invalid = [
            {"domain": value} for value in (
                "", "https://example.com", "example.com/path", "example.com:443",
                "example.com\nadmin off", "*.example.com", "a,b.com", "a_{x}.com",
                "-example.com", "example-.com", "a..com", "éxample.com",
                "a" * 64 + ".com", " example.com", "127.0.0.1",
            )
        ]
        invalid += [{"http_port": value} for value in (0, 65536, "80", True)]
        invalid += [{"https_port": value} for value in (0, 65536, "443", True)]
        invalid += [{"workers": value} for value in (0, -1, 33, "2", True)]
        invalid.append({"http_port": 8443, "https_port": 8443})
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.module.prepare_deployment(self.directory, **arguments)
                self.assertFalse(self.directory.exists())

    def test_rejects_directories_inside_git_repositories_and_linked_worktrees(self):
        for marker_is_file in (False, True):
            with self.subTest(linked_worktree=marker_is_file):
                root = self.base / str(marker_is_file)
                root.mkdir()
                marker = root / ".git"
                if marker_is_file:
                    marker.write_text("gitdir: /another/private/git/worktrees/test\n")
                else:
                    marker.mkdir()
                destination = root / "nested" / "private"
                with self.assertRaises(ValueError):
                    self.module.prepare_deployment(destination)
                self.assertFalse(destination.exists())

    def test_never_overwrites_existing_credentials_or_configuration(self):
        for name in (
            "access-token.txt", "access-token.sha256", "internal-token.txt",
            "redis-password.txt", "redis.acl", "public.env",
        ):
            with self.subTest(name=name):
                destination = self.base / name.replace(".", "-")
                destination.mkdir()
                previous = destination / name
                previous.write_text("preserve this existing operator file")
                with self.assertRaises(FileExistsError):
                    self.module.prepare_deployment(destination)
                self.assertEqual(previous.read_text(), "preserve this existing operator file")
                self.assertEqual(list(destination.iterdir()), [previous])

    def test_cli_displays_only_paths_and_commands_without_generated_credentials(self):
        completed = subprocess.run(
            [sys.executable, "-m", "taip.deployment.setup_public", "--directory",
             str(self.directory), "--bind-address", "0.0.0.0"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(str(self.directory / "public.env"), completed.stdout)
        self.assertIn("docker compose", completed.stdout)
        self.assertIn("compose.public.yaml", completed.stdout)
        self.assertIn("TAIP_BIND_ADDRESS=0.0.0.0\n", (self.directory / "public.env").read_text())
        for name in ("access-token.txt", "internal-token.txt", "redis-password.txt"):
            credential = (self.directory / name).read_text().strip()
            self.assertNotIn(credential, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
