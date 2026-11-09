"""Parse live opt-in Compose overlays without an engine or provider calls."""

import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from taip.deployment.setup_public import prepare_deployment


REPOSITORY = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")
PROVIDERS = ("GEMINI", "GROQ", "OPENROUTER")


@unittest.skipUnless(DOCKER, "Docker CLI is needed to parse Compose")
class LiveAIComposeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.public_env = prepare_deployment(self.base / "private deployment")
        docker_config = self.base / "empty docker config"
        docker_config.mkdir()
        # Docker Desktop's bundled plugin discovery depends on the ordinary
        # profile configuration. Supply only its installation directory.
        (docker_config / "config.json").write_text(json.dumps({
            "cliPluginsExtraDirs": [str(Path(DOCKER).parent.parent / "cli-plugins")],
        }), encoding="utf8")
        # Only synthetic test settings enter the child; never inherit vendor
        # credentials, a real .env file, or the user's Docker credential store.
        self.environment = {
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "DOCKER_CONFIG": str(docker_config),
            "USERPROFILE": str(self.base), "HOME": str(self.base),
            "APPDATA": str(self.base), "LOCALAPPDATA": str(self.base),
        }
        self.keys = {}
        for provider in PROVIDERS:
            key = self.base / (provider.lower() + "-fake-key.txt")
            key.write_text("synthetic-never-in-compose-" + provider, encoding="utf8")
            self.keys[provider] = key
            self.environment[provider + "_MODEL"] = "vendor/exact-" + provider.lower()
            self.environment[provider + "_API_KEY_FILE"] = str(key)

    def render(self, stack, *, live=True, environment=None):
        command = [DOCKER, "compose", "--project-directory", str(REPOSITORY)]
        if stack == "public":
            command.extend(("--env-file", str(self.public_env)))
        else:
            command.extend(("--env-file", str(self.base / "empty.env")))
            (self.base / "empty.env").touch()
        command.extend(("-f", str(REPOSITORY / ("compose.public.yaml" if stack == "public" else "compose.yaml"))))
        if live:
            command.extend(("-f", str(REPOSITORY / ("compose.public.ai.yaml" if stack == "public" else "compose.local.ai.yaml"))))
        command.extend(("config", "--format", "json"))
        return subprocess.run(command, env=dict(self.environment if environment is None else environment),
                              capture_output=True, text=True, check=False, timeout=30)

    def config(self, stack, **options):
        result = self.render(stack, **options)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_public_live_opt_in_preserves_auth_secrets_and_limits_vendor_keys_to_core(self):
        config = self.config("public")
        core = config["services"]["core"]
        gateway = config["services"]["gateway"]
        self.assertEqual(core["environment"]["TAIP_LIVE_AI"], "true")
        expected_auth = {"access_token_hash", "internal_token", "redis_password"}
        self.assertEqual({item["source"] for item in core["secrets"]}, expected_auth | {provider.lower() + "_api_key" for provider in PROVIDERS})
        self.assertEqual({item["source"] for item in gateway["secrets"]}, expected_auth)
        self.assertFalse(any(name.startswith(PROVIDERS) for name in gateway["environment"]))
        self.assertNotIn("secrets", config["services"]["web"])
        self.assertNotIn("ports", core)
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(core["environment"][provider + "_MODEL"], "vendor/exact-" + provider.lower())
                self.assertEqual(core["environment"][provider + "_JSON_SCHEMA"], "false")
                self.assertEqual(core["environment"][provider + "_API_KEY_FILE"], "/run/secrets/" + provider.lower() + "_api_key")
                self.assertNotIn(provider + "_API_KEY", core["environment"])
                self.assertEqual(Path(config["secrets"][provider.lower() + "_api_key"]["file"]), self.keys[provider])
                self.assertNotIn("synthetic-never-in-compose-" + provider, json.dumps(config))

    def test_local_live_opt_in_keeps_loopback_and_mounts_only_file_credentials(self):
        environment = {**self.environment, "GROQ_JSON_SCHEMA": "true"}
        config = self.config("local", environment=environment)
        api = config["services"]["api"]
        self.assertEqual(api["environment"]["TAIP_LIVE_AI"], "true")
        self.assertEqual(api["environment"]["GROQ_JSON_SCHEMA"], "true")
        self.assertEqual(api["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(config["services"]["web"]["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual({item["source"] for item in api["secrets"]}, {provider.lower() + "_api_key" for provider in PROVIDERS})
        self.assertNotIn("secrets", config["services"]["web"])
        self.assertFalse(any(name.endswith("_API_KEY") for name in api["environment"]))

    def test_each_exact_model_and_key_file_path_is_required_for_both_live_stacks(self):
        for stack in ("local", "public"):
            for provider in PROVIDERS:
                for suffix in ("_MODEL", "_API_KEY_FILE"):
                    for value in (None, ""):
                        with self.subTest(stack=stack, field=provider + suffix, value=value):
                            environment = dict(self.environment)
                            if value is None:
                                environment.pop(provider + suffix)
                            else:
                                environment[provider + suffix] = value
                            result = self.render(stack, environment=environment)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertNotIn("synthetic-never-in-compose", result.stdout + result.stderr)

    def test_base_stacks_stay_mock_without_provider_secret_mounts(self):
        for stack, service in (("public", "core"), ("local", "api")):
            with self.subTest(stack=stack):
                config = self.config(stack, live=False)
                self.assertEqual(config["services"][service]["environment"]["TAIP_LIVE_AI"], "false")
                self.assertTrue(all(not name.endswith("_api_key") for name in config.get("secrets", {})))


if __name__ == "__main__":
    unittest.main()
