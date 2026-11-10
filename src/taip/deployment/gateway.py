"""Stateless authenticated gateway; all mutable workflows stay in one core."""

import asyncio
import json
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx
from starlette.types import Receive, Scope, Send

from taip.ai_integration.errors import AIProviderError
from taip.ai_integration.rate_limits import RateLimitPolicy, RateLimitPort
from taip.deployment import asgi
from taip.deployment.auth import CredentialVerifier, origin_is_allowed
from taip.deployment.browser_sessions import BrowserSessionPort, RedisBrowserSessions, SESSION_TTL_SECONDS, read_browser_cookie
from taip.deployment.ingress import IngressPort, LocalIngressLimiter, RedisIngressLimiter
from taip.deployment.launcher_page import launcher_page
from taip.deployment.settings import DeploymentConfigurationError, DeploymentSettings, load_deployment_settings


_KEY = "global-http-admission"
_REQUEST_HEADERS = {b"accept", b"content-type", b"accept-language"}
_RESPONSE_HEADERS = {b"content-type", b"content-language", b"content-disposition"}


def _browser_response(payload: dict[str, object], *, cookie: str | None = None) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(payload, separators=(",", ":")).encode("ascii")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
               (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]
    if cookie is not None:
        headers.append((b"set-cookie", cookie.encode("ascii")))
    return 200, headers, body


class ProtectedGateway:
    def __init__(self, settings: DeploymentSettings, limiter: RateLimitPort,
                 client: httpx.AsyncClient, redis_client=None, *, ingress: IngressPort | None = None,
                 browser_sessions: BrowserSessionPort | None = None) -> None:
        self.settings = settings
        self._limiter = limiter
        self._client = client
        self._redis = redis_client
        self._browser = browser_sessions
        self._closed = False
        self._ingress = ingress if ingress is not None else LocalIngressLimiter(
            requests_per_minute=settings.http_ingress_requests_per_minute)
        self._ingress_slots = asyncio.BoundedSemaphore(settings.http_ingress_max_concurrent)
        self._ingress_retry_until = 0.0
        self._auth = CredentialVerifier(settings.access_token_digest, basic_username=settings.basic_username)
        self._authority = urlsplit(settings.public_origin).netloc.encode("ascii")
        self._policy = RateLimitPolicy(requests_per_minute=settings.http_requests_per_minute,
                                       requests_per_day=settings.http_requests_per_day,
                                       max_concurrent=settings.http_max_concurrent,
                                       tokens_per_minute=1000000000)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        # Do not abandon an admitted upstream operation on caller cancellation.
        # Its permit must remain held until the bounded operation actually ends.
        task = asyncio.create_task(self._handle(scope, receive))
        try:
            result = await asyncio.shield(task)
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
        except asgi.RequestRejected as error:
            await asgi.send_json(send, error.status_code, error.code,
                                 retry_after_seconds=error.retry_after_seconds,
                                 challenge="Basic" if error.status_code == 401 and not self.settings.launcher_login_enabled else None)
            return
        except Exception:
            await asgi.send_json(send, 503, "gateway_unavailable")
            return
        status, headers, body = result
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})

    def _validate_target(self, scope: Scope) -> tuple[bytes, bytes]:
        headers = scope.get("headers", ())
        if len(headers) > 100 or sum(len(k) + len(v) for k, v in headers) > 16384:
            raise asgi.RequestRejected(431, "headers_too_large")
        hosts = [v.lower() for k, v in headers if k.lower() == b"host"]
        if hosts != [self._authority]:
            raise asgi.RequestRejected(400, "invalid_host")
        raw = scope.get("raw_path", scope.get("path", "").encode("utf-8"))
        query = scope.get("query_string", b"")
        if len(raw) > 4096 or len(query) > 4096:
            raise asgi.RequestRejected(414, "target_too_large")
        try:
            text = raw.decode("ascii")
            decoded = unquote(text, errors="strict")
        except (UnicodeError, ValueError):
            raise asgi.RequestRejected(400, "invalid_target") from None
        if (not text.startswith("/") or text.startswith("//") or "\\" in decoded
                or any(c < " " or c == "\x7f" for c in decoded)
                or any(segment in (".", "..") for segment in decoded.split("/"))
                or "%2f" in text.lower() or "%5c" in text.lower()
                or "#" in text or "?" in text):
            raise asgi.RequestRejected(400, "invalid_target")
        return raw, query

    async def _browser_public(self, scope: Scope, receive: Receive, *, scheme: str) -> tuple | None:
        """Handle only the reserved local handoff routes, never proxy them."""
        path = scope["path"]
        if path != "/_launcher" and not path.startswith("/_launcher/"):
            return None
        if not self.settings.launcher_login_enabled:
            raise asgi.RequestRejected(404, "not_found")
        if scheme != "https":
            raise asgi.RequestRejected(403, "origin_not_allowed")
        if scope.get("query_string") or scope.get("raw_path", path.encode()) != path.encode():
            raise asgi.RequestRejected(400, "invalid_request")
        expected_method = {"/_launcher/": "GET", "/_launcher/code": "POST", "/_launcher/session": "POST"}.get(path)
        if expected_method is None:
            raise asgi.RequestRejected(404, "not_found")
        if scope["method"] != expected_method:
            raise asgi.RequestRejected(405, "method_not_allowed")
        if path == "/_launcher/":
            return launcher_page()
        if path == "/_launcher/code":
            return None  # Continue through explicit Bearer authentication and quota.
        headers = scope.get("headers", ())
        if not origin_is_allowed("POST", headers, scheme=scheme, expected_origin=self.settings.public_origin,
                                 authentication_kind="cookie"):
            raise asgi.RequestRejected(403, "origin_not_allowed")
        content_types = [v.lower() for k, v in headers if k.lower() == b"content-type"]
        if len(content_types) != 1 or content_types[0].split(b";", 1)[0].strip() != b"application/json":
            raise asgi.RequestRejected(415, "unsupported_media_type")
        body = await asgi.read_body(scope, receive, max_bytes=256, timeout_seconds=min(5, self.settings.body_timeout_seconds))
        payload = asgi.validate_json_payload(body, 256)
        if type(payload) is not dict or set(payload) != {"code"} or type(payload["code"]) is not str:
            raise asgi.RequestRejected(400, "invalid_request")
        token = await asyncio.to_thread(self._browser.exchange, payload["code"])
        if token is None:
            raise asgi.RequestRejected(401, "invalid_launch_code")
        cookie = f"{self._browser.cookie_name}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; Secure; HttpOnly; SameSite=Strict"
        return _browser_response({"ok": True}, cookie=cookie)

    async def _authenticate(self, headers) -> str:
        if any(k.lower() == b"authorization" for k, _ in headers):
            kind = self._auth.authenticate(headers)
            if kind is not None:
                return kind
        elif self.settings.launcher_login_enabled:
            token = read_browser_cookie(headers, self._browser.cookie_name)
            if token is not None and await asyncio.to_thread(self._browser.authenticate, token):
                return "cookie"
        raise asgi.RequestRejected(401, "authentication_required")

    async def _handle(self, scope: Scope, receive: Receive) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        if self._closed:
            raise asgi.RequestRejected(503, "gateway_unavailable")
        remaining = self._ingress_retry_until - monotonic()
        if remaining > 0:
            raise asgi.RequestRejected(429, "local_rate_limit", remaining)
        # Reject rather than enqueue admission I/O in the thread executor.
        # acquire() below cannot yield while this semaphore has a free slot.
        if self._ingress_slots.locked():
            raise asgi.RequestRejected(429, "local_rate_limit", 1)
        async with self._ingress_slots:
            try:
                await asyncio.to_thread(self._ingress.check)
            except AIProviderError as error:
                if error.code == "local_rate_limit" and error.retry_after_seconds is not None:
                    self._ingress_retry_until = monotonic() + min(60, error.retry_after_seconds)
                raise asgi.RequestRejected(429 if error.code == "local_rate_limit" else 503,
                                           error.code, error.retry_after_seconds) from None
            if scope.get("path") == "/healthz" and scope["method"] in ("GET", "HEAD"):
                try:
                    await asyncio.to_thread(self._limiter.snapshot, _KEY)
                except Exception:
                    raise asgi.RequestRejected(503, "admission_unavailable") from None
                return 200, [(b"content-type", b"application/json")], b'{"status":"ok"}'
            raw, query = self._validate_target(scope)
            headers = scope.get("headers", ())
            scheme = "https" if self.settings.trust_tls_proxy else scope.get("scheme", "")
            browser_result = await self._browser_public(scope, receive, scheme=scheme)
            if browser_result is not None:
                return browser_result
            mint = scope["path"] == "/_launcher/code"
            if mint:
                if self._auth.authenticate(headers) != "bearer":
                    raise asgi.RequestRejected(401, "authentication_required")
                kind = "bearer"
            else:
                kind = await self._authenticate(headers)
            if not origin_is_allowed(scope["method"], headers, scheme=scheme,
                                     expected_origin=self.settings.public_origin,
                                     authentication_kind="cookie" if kind == "cookie" else None):
                raise asgi.RequestRejected(403, "origin_not_allowed")
            try:
                permit = await asyncio.to_thread(self._limiter.acquire, _KEY, policy=self._policy,
                                                 estimated_tokens=1, provider="api", model_id="gateway")
            except AIProviderError as error:
                raise asgi.RequestRejected(429 if error.code == "local_rate_limit" else 503,
                                           error.code, error.retry_after_seconds) from None
        try:
            body = await asgi.read_body(scope, receive, max_bytes=0 if mint else self.settings.max_body_bytes,
                                        timeout_seconds=self.settings.body_timeout_seconds)
            if mint:
                return _browser_response({"code": await asyncio.to_thread(self._browser.mint)})
            core = scope["path"] == "/api" or scope["path"].startswith("/api/")
            url = httpx.URL(self.settings.core_url if core else self.settings.web_url).copy_with(
                raw_path=raw + (b"?" + query if query else b""))
            forwarded = [(k, v) for k, v in headers if k.lower() in _REQUEST_HEADERS]
            forwarded.append((b"accept-encoding", b"identity"))
            if core:
                forwarded.append((b"authorization", ("Bearer " + self.settings.internal_token).encode("ascii")))
            try:
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    async with self._client.stream(scope["method"], url, headers=forwarded, content=body) as response:
                        if (300 <= response.status_code < 400 or response.status_code >= 500
                                or core and response.status_code in (401, 403)
                                or response.headers.get("content-encoding", "identity").lower() != "identity"):
                            raise asgi.RequestRejected(502, "upstream_error")
                        output = bytearray()
                        async for part in response.aiter_bytes():
                            if len(part) > self.settings.max_response_bytes - len(output):
                                raise asgi.RequestRejected(502, "response_too_large")
                            output.extend(part)
                        response_headers = [(k, v) for k, v in response.headers.raw if k.lower() in _RESPONSE_HEADERS]
                        response_headers.extend([(b"content-length", str(len(output)).encode()),
                                                 (b"cache-control", b"no-store"),
                                                 (b"x-content-type-options", b"nosniff"),
                                                 (b"x-frame-options", b"DENY"),
                                                 (b"referrer-policy", b"no-referrer")])
                        return response.status_code, response_headers, bytes(output)
            except (TimeoutError, httpx.TimeoutException):
                raise asgi.RequestRejected(504, "upstream_timeout") from None
            except httpx.HTTPError:
                raise asgi.RequestRejected(502, "upstream_unavailable") from None
        finally:
            try:
                await asyncio.to_thread(permit.finish, actual_tokens=1)
            except Exception:
                raise asgi.RequestRejected(503, "admission_unavailable") from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()
        if self._redis is not None:
            await asyncio.to_thread(self._redis.close)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if self._redis is not None:
                        await asyncio.to_thread(self._redis.ping)
                except Exception:
                    await self.aclose()
                    await send({"type": "lifespan.startup.failed", "message": "Protected gateway startup failed."})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return


def create_gateway_app(settings: DeploymentSettings | None = None, *, limiter: RateLimitPort | None = None,
                       transport: httpx.AsyncBaseTransport | None = None, redis_client=None,
                       ingress: IngressPort | None = None, browser_sessions: BrowserSessionPort | None = None) -> ProtectedGateway:
    settings = load_deployment_settings() if settings is None else settings
    owned_client = None
    if limiter is None:
        from taip.deployment.redis_connection import create_redis_client
        from taip.deployment.redis_limits import RedisRateLimiter
        owned_client = create_redis_client(settings.redis) if redis_client is None else None
        limiter = RedisRateLimiter(redis_client if redis_client is not None else owned_client,
                                   namespace=settings.redis.namespace + ":http", lease_seconds=settings.redis_lease_seconds)
    ingress_client = redis_client if redis_client is not None else owned_client
    if settings.launcher_login_enabled and browser_sessions is None:
        if ingress_client is None:
            raise DeploymentConfigurationError("launcher_login_enabled")
        try:
            browser_sessions = RedisBrowserSessions(ingress_client, namespace=settings.redis.namespace,
                public_origin=settings.public_origin, access_token_digest=settings.access_token_digest)
        except Exception:
            if owned_client is not None:
                owned_client.close()
            raise
    if ingress is None and ingress_client is not None:
        ingress = RedisIngressLimiter(ingress_client, namespace=settings.redis.namespace,
                                       requests_per_minute=settings.http_ingress_requests_per_minute)
    client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                               timeout=settings.request_timeout_seconds,
                               limits=httpx.Limits(max_connections=settings.http_max_concurrent, max_keepalive_connections=16))
    return ProtectedGateway(settings, limiter, client, owned_client, ingress=ingress, browser_sessions=browser_sessions)
