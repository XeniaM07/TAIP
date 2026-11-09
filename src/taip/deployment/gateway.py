"""Stateless authenticated gateway; all mutable workflows stay in one core."""

import asyncio
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx
from starlette.types import Receive, Scope, Send

from taip.ai_integration.errors import AIProviderError
from taip.ai_integration.rate_limits import RateLimitPolicy, RateLimitPort
from taip.deployment import asgi
from taip.deployment.auth import CredentialVerifier, origin_is_allowed
from taip.deployment.ingress import IngressPort, LocalIngressLimiter, RedisIngressLimiter
from taip.deployment.settings import DeploymentSettings, load_deployment_settings


_KEY = "global-http-admission"
_REQUEST_HEADERS = {b"accept", b"content-type", b"accept-language"}
_RESPONSE_HEADERS = {b"content-type", b"content-language", b"content-disposition"}


class ProtectedGateway:
    def __init__(self, settings: DeploymentSettings, limiter: RateLimitPort,
                 client: httpx.AsyncClient, redis_client=None, *, ingress: IngressPort | None = None) -> None:
        self.settings = settings
        self._limiter = limiter
        self._client = client
        self._redis = redis_client
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
                                 challenge="Basic" if error.status_code == 401 else None)
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
            if self._auth.authenticate(headers) is None:
                raise asgi.RequestRejected(401, "authentication_required")
            scheme = "https" if self.settings.trust_tls_proxy else scope.get("scheme", "")
            if not origin_is_allowed(scope["method"], headers, scheme=scheme,
                                     expected_origin=self.settings.public_origin):
                raise asgi.RequestRejected(403, "origin_not_allowed")
            try:
                permit = await asyncio.to_thread(self._limiter.acquire, _KEY, policy=self._policy,
                                                 estimated_tokens=1, provider="api", model_id="gateway")
            except AIProviderError as error:
                raise asgi.RequestRejected(429 if error.code == "local_rate_limit" else 503,
                                           error.code, error.retry_after_seconds) from None
        try:
            body = await asgi.read_body(scope, receive, max_bytes=self.settings.max_body_bytes,
                                        timeout_seconds=self.settings.body_timeout_seconds)
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
                       ingress: IngressPort | None = None) -> ProtectedGateway:
    settings = load_deployment_settings() if settings is None else settings
    owned_client = None
    if limiter is None:
        from taip.deployment.redis_connection import create_redis_client
        from taip.deployment.redis_limits import RedisRateLimiter
        owned_client = create_redis_client(settings.redis) if redis_client is None else None
        limiter = RedisRateLimiter(redis_client if redis_client is not None else owned_client,
                                   namespace=settings.redis.namespace + ":http", lease_seconds=settings.redis_lease_seconds)
    ingress_client = redis_client if redis_client is not None else owned_client
    if ingress is None and ingress_client is not None:
        ingress = RedisIngressLimiter(ingress_client, namespace=settings.redis.namespace,
                                       requests_per_minute=settings.http_ingress_requests_per_minute)
    client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                               timeout=settings.request_timeout_seconds,
                               limits=httpx.Limits(max_connections=settings.http_max_concurrent, max_keepalive_connections=16))
    return ProtectedGateway(settings, limiter, client, owned_client, ingress=ingress)
