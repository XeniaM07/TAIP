"""ASGI session operations finish before another caller can mutate state."""

import asyncio
import unittest
from types import SimpleNamespace
from uuid import uuid4


class SessionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_session_is_busy_until_response_serialization_finishes(self):
        from taip.application.session_guard import SessionOperationGuard
        entered, release = asyncio.Event(), asyncio.Event()
        async def inner(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        workflow = SimpleNamespace(sessions=SimpleNamespace(get=lambda session_id: object()))
        guard = SessionOperationGuard(inner, workflow=workflow)
        scope = {"type": "http", "path": "/api/sessions/" + str(uuid4()), "method": "GET"}
        async def receive(): return {"type": "http.request", "body": b""}
        first, second = [], []
        async def send_first(message): first.append(message)
        async def send_second(message): second.append(message)
        task = asyncio.create_task(guard(scope, receive, send_first))
        await entered.wait()
        await guard(scope, receive, send_second)
        self.assertEqual(second[0]["status"], 409)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(len(guard._active), 0)

    async def test_unknown_ids_do_not_allocate_operation_slots(self):
        from taip.application.session_guard import SessionOperationGuard
        async def inner(scope, receive, send): self.fail("Unknown session reached workflow")
        def missing(value): raise KeyError
        guard = SessionOperationGuard(inner, workflow=SimpleNamespace(sessions=SimpleNamespace(get=missing)))
        messages = []
        async def receive(): return {"type": "http.request", "body": b""}
        async def send(message): messages.append(message)
        await guard({"type": "http", "path": "/api/sessions/" + str(uuid4())}, receive, send)
        self.assertEqual(messages[0]["status"], 404)
        self.assertEqual(len(guard._active), 0)
