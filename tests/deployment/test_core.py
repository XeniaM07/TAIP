"""Offline core boundaries exercise the real workflow and controlled concurrency."""

import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import importlib
from importlib import util
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from taip.application.workflow import (
    MOCK_DEMO_PROMPT, MOCK_MODEL_IDS, InMemorySessionRepository, MockProviderFactory,
    WorkflowService, build_default_service,
)
from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.rate_limits import RateLimiter
from taip.deployment.settings import DeploymentSettings, RedisSettings
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


INTERNAL_TOKEN = "internal_controlled_token_" + "a" * 32
PUBLIC_TOKEN = "public_controlled_token_" + "b" * 32
REDIS_PASSWORD = "redis_controlled_password_" + "c" * 32


def settings(**changes):
    return replace(DeploymentSettings(
        public_origin="https://controlled.test", access_token_digest=hashlib.sha256(PUBLIC_TOKEN.encode()).digest(),
        internal_token=INTERNAL_TOKEN, redis=RedisSettings(password=REDIS_PASSWORD),
    ), **changes)


class FakeOwner:
    def __init__(self):
        self.current = True
        self.started = 0
        self.closed = 0

    def is_current(self):
        return self.current

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1


class FakeRedis:
    def __init__(self):
        self.available = True
        self.closed = 0

    def ping(self):
        if not self.available:
            raise OSError("PRIVATE backend location")
        return True

    def close(self):
        self.closed += 1


class BlockingWorkflow(WorkflowService):
    def __init__(self):
        super().__init__(InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier())
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = False

    def extract(self, *args):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Controlled test did not release workflow")
        if self.fail:
            raise RuntimeError("PRIVATE workflow exception")
        return super().extract(*args)


class CoreBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        name = "taip.deployment.core"
        self.assertIsNotNone(util.find_spec(name), "Protected core module must exist")
        self.module = importlib.import_module(name)
        self.owner = FakeOwner()
        self.redis = FakeRedis()
        self.service = build_default_service()
        self.app = self.make_app()
        self.client = self.make_client(self.app)

    def make_app(self, service=None, **kwargs):
        return self.module.create_core_app(
            self.service if service is None else service, settings(),
            redis_client=self.redis, owner=self.owner, **kwargs,
        )

    def make_client(self, app):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://private-core",
                                 headers={"Authorization": "Bearer " + INTERNAL_TOKEN})

    async def asyncTearDown(self):
        await self.client.aclose()

    async def create_session(self, client=None):
        response = await (self.client if client is None else client).post(
            "/api/sessions", json={"prompt": MOCK_DEMO_PROMPT}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session_id"]

    async def test_minimal_health_is_exempt_but_workflow_requires_internal_bearer(self):
        health = await self.client.get("/api/health", headers={"Authorization": ""})
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        for credential in ("", "Bearer " + PUBLIC_TOKEN,
                           "Basic " + base64.b64encode(("taip:" + INTERNAL_TOKEN).encode()).decode()):
            result = await self.client.post("/api/sessions", json={"prompt": "controlled"},
                                            headers={"Authorization": credential})
            self.assertEqual(result.status_code, 401)
            self.assertNotIn(INTERNAL_TOKEN, result.text)
        await self.create_session()

    async def test_redis_ownership_checks_leave_the_event_loop_responsive(self):
        entered, release = threading.Event(), threading.Event()

        def slow_check():
            entered.set()
            release.wait(0.5)
            return True

        self.owner.is_current = slow_check
        task = asyncio.create_task(self.client.get("/api/health"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertFalse(task.done(), "The event loop must observe the in-flight Redis read")
        finally:
            release.set()
            await task

    async def test_internal_http_supports_complete_existing_mock_workflow(self):
        session_id = await self.create_session()
        extracted = await self.client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": None})
        self.assertEqual(extracted.status_code, 200, extracted.text)
        for action in ("confirm", "solve", "verify"):
            response = await self.client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/{action}")
            self.assertEqual(response.status_code, 200, response.text)
        summary = await self.client.get(f"/api/sessions/{session_id}/compare")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["models"][0]["verification_status"], "verified")

    async def test_unavailable_owner_fails_closed_for_health_and_workflow(self):
        self.owner.current = False
        for method, path in (("GET", "/api/health"), ("POST", "/api/sessions")):
            response = await self.client.request(method, path, json={"prompt": "controlled"})
            self.assertEqual(response.status_code, 503)

    async def test_owner_loss_while_receiving_body_blocks_workflow_mutation(self):
        async def body():
            self.owner.current = False
            yield b'{"prompt":"controlled"}'

        response = await self.client.post("/api/sessions", content=body(),
                                           headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.service.sessions._sessions, {})

    async def test_unknown_noncanonical_and_expired_session_ids_never_allocate_registry_entries(self):
        for session_id in ("missing", str(uuid4()).upper(), str(uuid4()), ""):
            result = await self.client.get(f"/api/sessions/{session_id}")
            self.assertEqual(result.status_code, 404)
            self.assertEqual(len(self.app._active_sessions), 0)
        expired = WorkflowService(InMemorySessionRepository(ttl=timedelta(seconds=-1)),
                                  MockProviderFactory(), TimetableVerifier())
        expired_id = expired.create_session("controlled").session_id
        async with self.make_client(self.make_app(expired)) as client:
            result = await client.get(f"/api/sessions/{expired_id}")
        self.assertEqual(result.status_code, 404)

    async def test_oversized_prompt_invalid_json_and_huge_integers_never_create_sessions(self):
        for body in (b'{"prompt":' + b"9" * 5000 + b"}", b'{"prompt":"x","prompt":"y"}',
                     b'{"prompt":NaN}', b"[1,2,3]", b'{"prompt":"\\ud800"}'):
            result = await self.client.post("/api/sessions", content=body,
                                            headers={"Content-Type": "application/json"})
            self.assertEqual(result.status_code, 400, result.text)
            self.assertNotIn("Traceback", result.text)
        oversized = await self.client.post("/api/sessions", json={"prompt": "ș" * 8193})
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.service.sessions._sessions, {})

    async def test_draft_patch_uses_canonical_codec_before_mutation(self):
        session_id = await self.create_session()
        await self.client.post(f"/api/sessions/{session_id}/extract")
        path = f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}"
        draft = (await self.client.get(path)).json()
        for value in (10**8, True, "2"):
            draft["data"]["requirements"][0]["sessions_per_week"] = value
            response = await self.client.patch(path, json={"data": draft["data"]})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.service.sessions.get(session_id).models[MOCK_MODEL_IDS[0]].edit_count, 0)
        for value in ({}, {"data": []}, {"data": {"unknown": 1}}):
            response = await self.client.patch(path, json=value)
            self.assertEqual(response.status_code, 400)

    async def test_extract_rejects_duplicate_unknown_or_malformed_model_selection_before_provider_calls(self):
        class FailingFactory:
            MODEL_IDS = MOCK_MODEL_IDS

            def __init__(self):
                self.calls = 0

            def get_provider(self, model_id):
                return self

            def extract(self, problem):
                self.calls += 1
                raise ValueError("Controlled provider failure")

        factory = FailingFactory()
        self.service.providers = factory
        session_id = await self.create_session()
        path = f"/api/sessions/{session_id}/extract"
        invalid_payloads = (
            {"model_ids": [MOCK_MODEL_IDS[0], MOCK_MODEL_IDS[0]]},
            {"model_ids": [MOCK_MODEL_IDS[0]] * 100},
            {"model_ids": ["unknown-model"]}, {"model_ids": [True]},
            {"model_ids": [1]}, {"model_ids": [None]}, {"model_ids": "gemini-mock"},
            {"model_ids": {}}, {"model_ids": True}, [], True, "models",
        )
        for payload in invalid_payloads:
            with self.subTest(kind=type(payload)):
                response = await self.client.post(path, json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(factory.calls, 0)
                session = self.service.sessions.get(session_id)
                self.assertTrue(all(workspace.status.value == "PENDING" for workspace in session.models.values()))

    async def test_extract_preserves_unique_subset_and_default_all_model_selections(self):
        for payload in (None, {}, {"model_ids": None}, {"model_ids": []},
                        {"model_ids": [MOCK_MODEL_IDS[0]]}):
            with self.subTest(payload=payload):
                session_id = await self.create_session()
                kwargs = {} if payload is None else {"json": payload}
                result = await self.client.post(f"/api/sessions/{session_id}/extract", **kwargs)
                self.assertEqual(result.status_code, 200, result.text)
                states = result.json()["models"]
                self.assertEqual(states[MOCK_MODEL_IDS[0]]["status"], "DRAFT")
                self.assertEqual(states[MOCK_MODEL_IDS[1]]["status"],
                                 "PENDING" if payload == {"model_ids": [MOCK_MODEL_IDS[0]]} else "DRAFT")

    async def test_same_session_reads_and_mutations_are_rejected_while_other_sessions_progress(self):
        service = BlockingWorkflow()
        app = self.make_app(service)
        async with self.make_client(app) as client:
            first = await self.create_session(client)
            second = await self.create_session(client)
            task = asyncio.create_task(client.post(f"/api/sessions/{first}/extract"))
            self.assertTrue(await asyncio.to_thread(service.entered.wait, 2))
            try:
                for path in (f"/api/sessions/{first}", f"/api/sessions/{first}/compare"):
                    result = await client.get(path)
                    self.assertEqual(result.status_code, 409)
                self.assertEqual((await client.get(f"/api/sessions/{second}")).status_code, 200)
            finally:
                service.release.set()
                self.assertEqual((await task).status_code, 200)
            self.assertEqual(len(app._active_sessions), 0)

    async def test_client_cancellation_keeps_session_reserved_until_sync_work_finishes(self):
        service = BlockingWorkflow()
        app = self.make_app(service)
        async with self.make_client(app) as client:
            session_id = await self.create_session(client)
            task = asyncio.create_task(client.post(f"/api/sessions/{session_id}/extract"))
            self.assertTrue(await asyncio.to_thread(service.entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            try:
                self.assertEqual((await client.get(f"/api/sessions/{session_id}")).status_code, 409)
            finally:
                service.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual((await client.get(f"/api/sessions/{session_id}")).status_code, 200)
            self.assertEqual(len(app._active_sessions), 0)

    async def test_registry_is_released_after_inner_failure_without_exposing_details(self):
        service = BlockingWorkflow()
        service.fail = True
        service.release.set()
        app = self.make_app(service)
        async with self.make_client(app) as client:
            session_id = await self.create_session(client)
            response = await client.post(f"/api/sessions/{session_id}/extract")
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("PRIVATE", response.text)
            self.assertEqual((await client.get(f"/api/sessions/{session_id}")).status_code, 200)
        self.assertEqual(len(app._active_sessions), 0)

    async def test_complete_inner_response_is_buffered_before_session_unlock(self):
        session_id = await self.create_session()
        inner = self.app.inner
        completed = asyncio.Event()
        release = asyncio.Event()

        async def delayed(scope, receive, send):
            await inner(scope, receive, send)
            completed.set()
            await release.wait()

        self.app.inner = delayed
        task = asyncio.create_task(self.client.get(f"/api/sessions/{session_id}"))
        await asyncio.wait_for(completed.wait(), 2)
        try:
            self.assertFalse(task.done())
            busy = await self.client.get(f"/api/sessions/{session_id}")
            self.assertEqual(busy.status_code, 409)
        finally:
            release.set()
            self.assertEqual((await task).status_code, 200)

    async def test_oversized_inner_response_is_replaced_and_registry_released(self):
        app = self.module.create_core_app(self.service, settings(max_response_bytes=8),
                                          redis_client=self.redis, owner=self.owner)
        session_id = self.service.create_session("controlled").session_id
        async with self.make_client(app) as client:
            result = await client.get(f"/api/sessions/{session_id}")
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("controlled", result.text)
        self.assertEqual(len(app._active_sessions), 0)

    async def test_lifespan_starts_owner_and_keeps_injected_redis_caller_owned(self):
        messages = iter(({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}))
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await self.app({"type": "lifespan"}, receive, send)
        self.assertEqual([item["type"] for item in sent], ["lifespan.startup.complete", "lifespan.shutdown.complete"])
        self.assertEqual((self.owner.started, self.owner.closed, self.redis.closed), (1, 1, 0))

    async def test_startup_redis_failure_is_safe_and_prevents_readiness(self):
        self.redis.available = False
        sent = []

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            sent.append(message)

        await self.app({"type": "lifespan"}, receive, send)
        self.assertEqual(sent[0]["type"], "lifespan.startup.failed")
        self.assertNotIn("PRIVATE", repr(sent))
        self.assertEqual((await self.client.get("/api/health")).status_code, 503)

    async def test_live_factory_uses_injected_distributed_limiter_and_rejects_short_leases(self):
        config = ProviderConfig("controlled-model", ProviderKind.GROQ, "controlled-exact-model", "synthetic-api-key", timeout_seconds=60)
        limiter = RateLimiter()
        with patch.object(self.module, "load_provider_configs", return_value=(config,)):
            with self.assertRaises(ValueError):
                self.module.create_core_app(settings=settings(live_ai=True, request_timeout_seconds=10, redis_lease_seconds=200),
                                             redis_client=self.redis, owner=self.owner, limiter=limiter)
            app = self.module.create_core_app(settings=settings(live_ai=True), redis_client=self.redis,
                                               owner=self.owner, limiter=limiter)
        self.assertIs(app.state.workflow.providers._limiter, limiter)
        self.assertEqual(app.state.workflow.providers.MODEL_IDS, ("controlled-model",))
        self.assertEqual(app._invocations.maxlen, 100)
        app._close_resources()


if __name__ == "__main__":
    unittest.main()
