"""Runtime discovery and mutation responses use safe, coherent session views."""

import asyncio
from threading import Event
import unittest

import httpx

from taip.ai_integration.errors import AIProviderError
from taip.application.api import create_app
from taip.application.workflow import (
    MOCK_DEMO_PROMPT, MOCK_MODEL_IDS, InMemorySessionRepository,
    MockProviderFactory, MockTimetableProvider, WorkflowService, WorkflowStatus,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


class ApiRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def service(self, providers=None):
        return WorkflowService(InMemorySessionRepository(), providers or MockProviderFactory(), TimetableVerifier())

    def client(self, app):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")

    async def test_default_runtime_discovery_exposes_mock_catalog_and_demo(self):
        async with self.client(create_app()) as client:
            response = await client.get("/api/runtime")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "mock")
        self.assertEqual([item["model_id"] for item in response.json()["models"]], list(MOCK_MODEL_IDS))
        self.assertEqual(response.json()["demo_prompt"], MOCK_DEMO_PROMPT)

    async def test_custom_catalog_uses_configured_ids_and_supplied_snapshot_callable(self):
        class CustomFactory:
            MODEL_IDS = ("custom-a", "custom-b")

            def get_provider(self, model_id):
                raise AssertionError("Discovery is read-only")

        service = self.service(CustomFactory())
        async with self.client(create_app(service)) as client:
            catalog = (await client.get("/api/runtime")).json()
            self.assertEqual((await client.get("/api/mock/demo-prompt")).status_code, 404)
        self.assertEqual(catalog["mode"], "custom")
        self.assertEqual([item["model_id"] for item in catalog["models"]], ["custom-a", "custom-b"])
        self.assertIsNone(catalog["demo_prompt"])
        snapshots = iter(({"mode": "custom", "models": [], "demo_prompt": None}, {"mode": "live", "models": [], "demo_prompt": None}))
        async with self.client(create_app(service, runtime_info=lambda: next(snapshots))) as client:
            self.assertEqual((await client.get("/api/runtime")).json()["mode"], "custom")
            self.assertEqual((await client.get("/api/runtime")).json()["mode"], "live")

    async def test_canonical_factory_without_catalog_uses_the_same_legacy_ids_as_sessions(self):
        class CanonicalFactory:
            def get_provider(self, model_id):
                return MockTimetableProvider(model_id)

        async with self.client(create_app(self.service(CanonicalFactory()))) as client:
            discovered = await client.get("/api/runtime")
            self.assertEqual(discovered.status_code, 200, discovered.text)
            catalog = discovered.json()
            self.assertEqual(catalog["mode"], "custom")
            self.assertIsNone(catalog["demo_prompt"])
            model_ids = [model["model_id"] for model in catalog["models"]]
            self.assertEqual(model_ids, list(MOCK_MODEL_IDS))
            created = await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})
            self.assertEqual(list(created.json()["models"]), model_ids)
            extracted = await client.post(f"/api/sessions/{created.json()['session_id']}/extract")
            self.assertEqual(extracted.status_code, 200, extracted.text)
            self.assertEqual(extracted.json()["status"], "DRAFT")
            self.assertEqual(list(extracted.json()["models"]), model_ids)

    async def test_mutations_optionally_return_updated_whole_session(self):
        service = self.service()
        async with self.client(create_app(service)) as client:
            session_id = (await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})).json()["session_id"]
            extracted = (await client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]})).json()
            data = extracted["models"][MOCK_MODEL_IDS[0]]["draft"]["data"]
            edited = await client.patch(f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}?session_view=true", json={"data": data})
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(edited.json()["session_id"], session_id)
            for action, expected in (("confirm", "CONFIRMED"), ("solve", "COMPLETED"), ("verify", "COMPLETED")):
                response = await client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/{action}?session_view=true")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["session_id"], session_id)
                self.assertEqual(response.json()["models"][MOCK_MODEL_IDS[0]]["status"], expected)
                self.assertEqual(len(response.json()["models"]), 3)
                self.assertNotEqual(response.json()["status"], "COMPLETED")

    async def test_known_provider_failure_returns_failed_state_with_retry_metadata(self):
        class FailingFactory(MockProviderFactory):
            def get_provider(self, model_id):
                provider = super().get_provider(model_id)

                def fail(snapshot):
                    raise AIProviderError("rate_limit", status_code=429, retry_after_seconds=12)

                provider.solve = fail
                return provider

        service = self.service(FailingFactory())
        async with self.client(create_app(service)) as client:
            session_id = (await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})).json()["session_id"]
            await client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]})
            await client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/confirm")
            response = await client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/solve?session_view=true")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "PARTIAL")
        workspace = response.json()["models"][MOCK_MODEL_IDS[0]]
        self.assertEqual(workspace["status"], "FAILED")
        self.assertEqual(workspace["failure"]["code"], "rate_limit")
        self.assertTrue(workspace["failure"]["retryable"])
        self.assertEqual(workspace["failure"]["retry_after_seconds"], 12)

    async def test_unexpected_service_error_stays_a_server_error(self):
        service = self.service()
        session = service.create_session(MOCK_DEMO_PROMPT)

        def fail(*args):
            raise RuntimeError("private-server-details")

        service.solve = fail
        async with self.client(create_app(service)) as client:
            response = await client.post(f"/api/sessions/{session.session_id}/models/{MOCK_MODEL_IDS[0]}/solve?session_view=true")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-server-details", response.text)

    async def test_unexpected_provider_validation_shaped_exceptions_are_safe_server_errors(self):
        for error_type in (ValueError, TypeError, KeyError):
            with self.subTest(error_type=error_type.__name__):
                class FailingFactory(MockProviderFactory):
                    def get_provider(self, model_id):
                        provider = super().get_provider(model_id)

                        def fail(snapshot):
                            raise error_type("private-server-details")

                        provider.solve = fail
                        return provider

                service = self.service(FailingFactory())
                async with self.client(create_app(service)) as client:
                    session_id = (await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})).json()["session_id"]
                    await client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]})
                    await client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/confirm")
                    response = await client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/solve?session_view=true")
                self.assertEqual(response.status_code, 500, response.text)
                self.assertNotIn("private-server-details", response.text)

    async def test_session_progress_does_not_treat_pending_models_as_completed(self):
        service = self.service()
        session = service.create_session(MOCK_DEMO_PROMPT)
        session.models[MOCK_MODEL_IDS[0]].status = WorkflowStatus.COMPLETED
        async with self.client(create_app(service)) as client:
            response = await client.get(f"/api/sessions/{session.session_id}")
        self.assertEqual(response.json()["status"], "PENDING")
        self.assertEqual(response.json()["progress"], {"total": 3, "pending": 2, "extracting": 0, "draft": 0, "confirmed": 0, "solving": 0, "completed": 1, "failed": 0})

    async def test_prompt_limit_counts_utf8_bytes_before_creating_a_session(self):
        service = self.service()
        async with self.client(create_app(service)) as client:
            response = await client.post("/api/sessions", json={"prompt": "ă" * 8193})
            self.assertEqual(response.status_code, 422)
            accepted = await client.post("/api/sessions", json={"prompt": "ă" * 8192})
            self.assertEqual(accepted.status_code, 201)

    async def test_draft_patch_rejects_wrong_scalar_types_and_unknown_collections(self):
        service = self.service()
        async with self.client(create_app(service)) as client:
            session_id = (await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})).json()["session_id"]
            extracted = (await client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]})).json()
            data = extracted["models"][MOCK_MODEL_IDS[0]]["draft"]["data"]
            data["classes"][0]["student_count"] = True
            response = await client.patch(f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}", json={"data": data})
            self.assertEqual(response.status_code, 400, response.text)
            data["classes"][0]["student_count"] = 24
            data["unknown_entities"] = []
            response = await client.patch(f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}", json={"data": data})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(service.sessions.get(session_id).models[MOCK_MODEL_IDS[0]].draft.data.classes[0].student_count, 24)

    async def test_local_api_rejects_overlap_until_full_session_response_finishes(self):
        entered, release = Event(), Event()

        class BlockingFactory(MockProviderFactory):
            def get_provider(self, model_id):
                provider = super().get_provider(model_id)
                original = provider.extract

                def extract(problem):
                    entered.set()
                    release.wait(3)
                    return original(problem)

                provider.extract = extract
                return provider

        service = self.service(BlockingFactory())
        async with self.client(create_app(service)) as client:
            session_id = (await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})).json()["session_id"]
            first = asyncio.create_task(client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                overlap = await client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]})
            finally:
                release.set()
                completed = await first
            self.assertEqual(overlap.status_code, 409, overlap.text)
            self.assertEqual(completed.status_code, 200)
            retry = await client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]})
            self.assertEqual(retry.status_code, 200)


if __name__ == "__main__":
    unittest.main()
