"""Serialize complete ASGI responses for one temporary workflow session."""

import asyncio
import json
from uuid import UUID

from starlette.types import ASGIApp, Receive, Scope, Send


async def _reject(send: Send, status: int, code: str) -> None:
    body = json.dumps({"code": code}, separators=(",", ":")).encode("ascii")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class SessionOperationGuard:
    def __init__(self, app: ASGIApp, *, workflow, max_active: int = 32) -> None:
        self.app = app
        self.workflow = workflow
        self._capacity = max_active
        self._active: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/sessions/"):
            await self.app(scope, receive, send)
            return
        session_id = path.split("/")[3]
        try:
            if str(UUID(session_id)) != session_id:
                raise ValueError
            await asyncio.to_thread(self.workflow.sessions.get, session_id)
        except (KeyError, ValueError):
            await _reject(send, 404, "not_found")
            return
        if session_id in self._active or len(self._active) >= self._capacity:
            await _reject(send, 409, "session_busy")
            return
        self._active.add(session_id)
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise
        finally:
            self._active.discard(session_id)
