"""Offline runtime composition, catalog privacy, and ownership checks."""

import asyncio
from importlib import import_module, util
import json
import unittest
from unittest.mock import patch

import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIConfigurationError
from taip.ai_integration.rate_limits import QuotaSnapshot, RateLimiter
from taip.application.workflow import MOCK_DEMO_PROMPT, MOCK_MODEL_IDS


class OfflineTransport:
    def __init__(self):
        self.calls = []
        self.closed = 0

    def post(self, **request):
        self.calls.append(request)
        raise AssertionError("Catalog and startup must never generate responses")

    def close(self):
        self.closed += 1


class RuntimeCompositionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(util.find_spec("taip.application.runtime"), "Explicit runtime composition must exist")
        self.module = import_module("taip.application.runtime")

    def configs(self):
        return tuple(ProviderConfig(
            model_id="logical-" + kind.value, provider=kind,
            api_model_id="vendor/exact-" + kind.value, api_key="fake-private-" + kind.value,
        ) for kind in ProviderKind)

    def test_mock_is_explicit_default_and_has_demo_without_network_resources(self):
        with patch.object(self.module, "HTTPJSONTransport", side_effect=AssertionError("No mock HTTP pool")):
            runtime = self.module.build_runtime()
            try:
                catalog = runtime.runtime_info()
                self.assertEqual(catalog["mode"], "mock")
                self.assertEqual(catalog["demo_prompt"], MOCK_DEMO_PROMPT)
                self.assertEqual([item["model_id"] for item in catalog["models"]], list(MOCK_MODEL_IDS))
                self.assertTrue(all(item["limits"] is None and item["quota"] is None for item in catalog["models"]))
                self.assertEqual(set(runtime.workflow.create_session("demo").models), set(MOCK_MODEL_IDS))
            finally:
                runtime.close()

    def test_live_catalog_reports_exact_configured_models_and_safe_quotas(self):
        transport = OfflineTransport()
        runtime = self.module.build_runtime(self.configs(), transport=transport)
        try:
            catalog = runtime.runtime_info()
            self.assertEqual(catalog["mode"], "live")
            self.assertIsNone(catalog["demo_prompt"])
            self.assertEqual(set(catalog), {"mode", "models", "demo_prompt"})
            self.assertEqual([item["model_id"] for item in catalog["models"]], ["logical-gemini", "logical-groq", "logical-openrouter"])
            item = catalog["models"][0]
            self.assertEqual(set(item), {"model_id", "label", "provider", "requested_model_id", "limits", "quota", "warnings"})
            self.assertEqual(item["requested_model_id"], "vendor/exact-gemini")
            self.assertEqual(item["quota"], {"requests_in_minute": 0, "requests_in_day": 0, "tokens_in_minute": 0, "in_flight": 0, "cooldown_seconds": 0.0})
            self.assertEqual(item["limits"], {"requests_per_minute": 6, "requests_per_day": 100, "tokens_per_minute": 60000, "max_concurrent": 2, "warning_fraction": 0.8})
            self.assertNotIn("fake-private", json.dumps(catalog))
            self.assertNotIn("quota_key", json.dumps(catalog))
            self.assertEqual(transport.calls, [])
            self.assertEqual(set(runtime.workflow.create_session("live").models), {"logical-gemini", "logical-groq", "logical-openrouter"})
        finally:
            runtime.close()
        self.assertEqual(transport.closed, 0)

    def test_quota_snapshot_failure_is_safe_and_isolated_per_model(self):
        class PartiallyUnavailableLimiter(RateLimiter):
            def snapshot(self, key):
                if key[0] == "groq":
                    raise RuntimeError("fake-private-backend-details")
                return QuotaSnapshot(5, 90, 0, 0, 7.5)

        runtime = self.module.build_runtime(self.configs(), transport=OfflineTransport(), limiter=PartiallyUnavailableLimiter())
        try:
            models = runtime.runtime_info()["models"]
            self.assertIsNone(models[1]["quota"])
            self.assertTrue(models[1]["warnings"])
            self.assertEqual(models[0]["quota"]["cooldown_seconds"], 7.5)
            self.assertTrue(models[0]["warnings"])
            self.assertNotIn("fake-private", json.dumps(models))
        finally:
            runtime.close()

    def test_close_is_idempotent_and_only_closes_owned_resources(self):
        from taip.orchestration.timetable_execution import TimetableExecutionCoordinator

        transport = OfflineTransport()
        external_execution = TimetableExecutionCoordinator(max_workers=1, max_pending=1)
        self.addCleanup(external_execution.close)
        with patch.object(self.module, "HTTPJSONTransport", return_value=transport):
            runtime = self.module.build_runtime(self.configs(), execution=external_execution)
        self.assertIs(runtime.execution, external_execution)
        runtime.close()
        runtime.close()
        self.assertEqual(transport.closed, 1)
        self.assertEqual(runtime.execution.execute("extract", {"still-open": lambda: "available"})["still-open"].result, "available")

    def test_invalid_live_configuration_does_not_fall_back_and_closes_owned_pool(self):
        transport = OfflineTransport()
        with patch.object(self.module, "HTTPJSONTransport", return_value=transport):
            with self.assertRaises(AIConfigurationError):
                self.module.build_runtime(())
        self.assertEqual(transport.closed, 1)

    def test_runtime_factory_reads_supplied_environment_only_at_invocation(self):
        with patch.object(self.module, "load_provider_configs", side_effect=AssertionError("Mock must not load credentials")):
            app = self.module.create_runtime_app({"TAIP_LIVE_AI": "false"})
            self.assertEqual(app.state.runtime.runtime_info()["mode"], "mock")
            app.state.runtime.close()
        with self.assertRaises(AIConfigurationError):
            self.module.create_runtime_app({"TAIP_LIVE_AI": "invalid"})
        with self.assertRaises(AIConfigurationError):
            self.module.create_runtime_app({"TAIP_LIVE_AI": "true"})

    def test_local_factory_closes_owned_execution_and_transport_during_shutdown(self):
        transport = OfflineTransport()
        with patch.object(self.module, "HTTPJSONTransport", return_value=transport), patch.object(
            self.module, "load_provider_configs", return_value=self.configs(),
        ):
            app = self.module.create_runtime_app({"TAIP_LIVE_AI": "true"})
        async def exercise_lifespan():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.get("/api/runtime")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["mode"], "live")
                    self.assertEqual(transport.closed, 0)

        asyncio.run(exercise_lifespan())
        self.assertEqual(transport.closed, 1)
        outcome = app.state.runtime.execution.execute("extract", {"after-shutdown": lambda: "unexpected"})["after-shutdown"]
        self.assertEqual(outcome.failure.code, "coordinator_busy")
        self.assertFalse(outcome.attempted)


if __name__ == "__main__":
    unittest.main()
