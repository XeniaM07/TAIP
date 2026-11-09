"""Private, single-owner workflow composition behind the distributed gateway."""

import asyncio
from collections import deque
from hashlib import sha256
from uuid import UUID

from taip.ai_integration.config import load_provider_configs
from taip.ai_integration.errors import AIResponseError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.ai_integration.rate_limits import RateLimitPort
from taip.ai_integration.request_guard import validate_source_prompt
from taip.application.api import create_app
from taip.application.workflow import InMemorySessionRepository, WorkflowService, build_default_service
from taip.deployment import asgi
from taip.deployment.auth import CredentialVerifier
from taip.deployment.redis_limits import RedisRateLimiter
from taip.deployment.settings import DeploymentConfigurationError, DeploymentSettings, load_deployment_settings
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


class ProtectedCore:
    """Keep mutable workflow state private and serialize complete session calls.

    This wrapper requires one Uvicorn worker. A Redis ownership lease prevents
    an accidental second core from publishing a divergent in-memory history.
    Redis stores admission and ownership metadata, never workflow sessions.
    """

    def __init__(self, service, settings, redis_client, owner, *, owned_client=False, transport=None, invocations=None):
        self.inner = create_app(service)
        self.state = self.inner.state
        self.settings = settings
        self._client = redis_client
        self._owner = owner
        self._owned_client = owned_client
        self._transport = transport
        self._invocations = invocations
        self._closed = False
        self._startup_failed = False
        self._active_sessions: set[str] = set()
        self._max_active_sessions = min(32, settings.http_max_concurrent)
        self._verifier = CredentialVerifier(sha256(settings.internal_token.encode("ascii")).digest(), allow_basic=False)

    def _current(self):
        if self._closed or self._startup_failed:
            return False
        try:
            return self._owner.is_current() is True
        except Exception:
            return False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if path == "/api/health" and method in ("GET", "HEAD"):
            if not await asyncio.to_thread(self._current):
                await asgi.send_json(send, 503, "core_unavailable")
            else:
                body = b'{"status":"ok"}'
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})
            return
        if self._verifier.authenticate(scope.get("headers", ())) != "bearer":
            await asgi.send_json(send, 401, "authentication_required")
            return
        if not await asyncio.to_thread(self._current):
            await asgi.send_json(send, 503, "core_unavailable")
            return
        session_id = None
        reserved = False
        try:
            session_id = self._session_id(path)
            body = await asgi.read_body(scope, receive, max_bytes=self.settings.max_body_bytes,
                                        timeout_seconds=self.settings.body_timeout_seconds)
            self._validate_payload(method, path, body)
            if session_id is not None:
                # No await occurs between testing and reserving the set entry.
                # Unknown or expired IDs were rejected before touching this set.
                if session_id in self._active_sessions or len(self._active_sessions) >= self._max_active_sessions:
                    raise asgi.RequestRejected(409, "session_busy")
                self._active_sessions.add(session_id)
                reserved = True
            if not await asyncio.to_thread(self._current):
                raise asgi.RequestRejected(503, "core_unavailable")
            messages = await self._run_inner(scope, body)
            if not await asyncio.to_thread(self._current):
                raise asgi.RequestRejected(503, "core_unavailable")
        except asgi.RequestRejected as error:
            await asgi.send_json(send, error.status_code, error.code,
                                 retry_after_seconds=error.retry_after_seconds)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            await asgi.send_json(send, 500, "internal_error")
            return
        finally:
            if reserved:
                self._active_sessions.discard(session_id)
        status = messages[0]["status"]
        if status >= 400:
            code = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed",
                    409: "session_busy", 422: "invalid_request"}.get(status, "internal_error")
            await asgi.send_json(send, 400 if status == 422 else status, code)
            return
        for message in messages:
            await send(message)

    def _session_id(self, path):
        if not path.startswith("/api/sessions/"):
            return None
        raw = path.split("/")[3]
        try:
            if str(UUID(raw)) != raw:
                raise ValueError
            self.state.workflow.sessions.get(raw)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise asgi.RequestRejected(404, "not_found") from None
        return raw

    def _validate_payload(self, method, path, body):
        payload = asgi.validate_json_payload(body, self.settings.max_body_bytes) if body else None
        try:
            if method == "POST" and path == "/api/sessions":
                if type(payload) is not dict:
                    raise asgi.RequestRejected(400, "invalid_request")
                validate_source_prompt(payload.get("prompt"), max_bytes=16384)
            parts = path.split("/")
            if (method == "POST" and len(parts) == 5 and parts[:3] == ["", "api", "sessions"]
                    and parts[4] == "extract"):
                if payload is not None and type(payload) is not dict:
                    raise asgi.RequestRejected(400, "invalid_request")
                selected = payload.get("model_ids") if payload is not None else None
                if selected is not None:
                    configured = self.state.workflow.providers.MODEL_IDS
                    if type(selected) is not list or len(selected) > len(configured):
                        raise asgi.RequestRejected(400, "invalid_request")
                    seen = set()
                    for model_id in selected:
                        if type(model_id) is not str or model_id not in configured or model_id in seen:
                            raise asgi.RequestRejected(400, "invalid_request")
                        seen.add(model_id)
            if method == "PATCH" and path.startswith("/api/sessions/") and "/drafts/" in path:
                if type(payload) is not dict or type(payload.get("data")) is not dict:
                    raise asgi.RequestRejected(400, "invalid_request")
                # Reuse canonical shape/resource validation before the existing
                # mutable workflow route receives its original JSON document.
                from taip.ai_integration.response_codec import decode_timetable_data
                decode_timetable_data(payload["data"])
        except AIResponseError as error:
            raise asgi.RequestRejected(413 if error.code == "input_too_large" else 400, "invalid_request") from None
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise asgi.RequestRejected(400, "invalid_request") from None

    async def _run_inner(self, scope, body):
        messages = []
        size = 0
        finished = False
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def capture(message):
            nonlocal size, finished
            kind = message.get("type")
            if finished or kind not in ("http.response.start", "http.response.body"):
                raise asgi.RequestRejected(500, "internal_error")
            if kind == "http.response.start":
                if messages:
                    raise asgi.RequestRejected(500, "internal_error")
                messages.append(dict(message))
                return
            if not messages:
                raise asgi.RequestRejected(500, "internal_error")
            content = message.get("body", b"")
            if type(content) is not bytes:
                raise asgi.RequestRejected(500, "internal_error")
            size += len(content)
            if size > self.settings.max_response_bytes:
                raise asgi.RequestRejected(502, "response_too_large")
            messages.append(dict(message))
            finished = not message.get("more_body", False)

        task = asyncio.create_task(self.inner(scope, replay, capture))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled HTTP caller must not release this session while a
            # synchronous workflow operation is still running in a worker.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                task.result()
            except BaseException:
                pass
            raise
        if not finished:
            raise asgi.RequestRejected(500, "internal_error")
        return messages

    async def _lifespan(self, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await asyncio.to_thread(self._start_resources)
                except Exception:
                    self._startup_failed = True
                    await asyncio.to_thread(self._close_resources)
                    await send({"type": "lifespan.startup.failed", "message": "Protected core startup failed."})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await asyncio.to_thread(self._close_resources)
                await send({"type": "lifespan.shutdown.complete"})
                return

    def _start_resources(self):
        if self._client.ping() is not True:
            raise RuntimeError("Protected core startup failed.")
        self._owner.start()
        if not self._current():
            raise RuntimeError("Protected core startup failed.")

    def _close_resources(self):
        if self._closed:
            return
        self._closed = True
        for resource in (self._owner, self._transport, self._client if self._owned_client else None):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def create_core_app(
    service: WorkflowService | None = None, settings: DeploymentSettings | None = None, *,
    redis_client=None, limiter: RateLimitPort | None = None, owner=None,
) -> ProtectedCore:
    """Build the private core at explicit startup; imports never read secrets."""
    settings = load_deployment_settings() if settings is None else settings
    if not isinstance(settings, DeploymentSettings):
        raise DeploymentConfigurationError("settings")
    owned_client = redis_client is None
    if owned_client:
        from taip.deployment.redis_connection import create_redis_client
        redis_client = create_redis_client(settings.redis)
    transport = None
    invocations = None
    try:
        if owner is None:
            from taip.deployment.ownership import CoreOwnershipLease
            owner = CoreOwnershipLease(redis_client, namespace=settings.redis.namespace)
        if service is None:
            if settings.live_ai:
                configs = load_provider_configs()
                if settings.redis_lease_seconds <= 4 * max(config.timeout_seconds for config in configs) + 10:
                    raise DeploymentConfigurationError("redis_lease_seconds")
                shared_limiter = limiter if limiter is not None else RedisRateLimiter(
                    redis_client, namespace=settings.redis.namespace + ":providers", lease_seconds=settings.redis_lease_seconds
                )
                invocations = deque(maxlen=100)
                transport = HTTPJSONTransport()
                factory = AIProviderFactory(configs, transport=transport, observer=invocations.append, limiter=shared_limiter)
                service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
            else:
                service = build_default_service()
        return ProtectedCore(service, settings, redis_client, owner, owned_client=owned_client,
                             transport=transport, invocations=invocations)
    except Exception:
        if transport is not None:
            transport.close()
        if owned_client:
            redis_client.close()
        raise
