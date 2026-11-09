"""Bounded ASGI input and safe, constant diagnostic responses."""

import asyncio
import json
import math
from typing import NoReturn

from starlette.types import Receive, Scope, Send

from taip.ai_integration.errors import AIResponseError
from taip.ai_integration.request_guard import validate_domain_input


class RequestRejected(Exception):
    def __init__(self, status_code: int, code: str, retry_after_seconds: float | None = None):
        self.status_code = status_code
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


async def read_body(scope: Scope, receive: Receive, *, max_bytes: int, timeout_seconds: float) -> bytes:
    lengths = [value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"]
    encodings = [value for key, value in scope.get("headers", ()) if key.lower() == b"transfer-encoding"]
    if encodings and (lengths or len(encodings) != 1 or encodings[0].lower() != b"chunked"):
        raise RequestRejected(400, "invalid_request")
    declared = None
    if lengths:
        raw = lengths[0]
        if len(lengths) != 1 or len(raw) > 20 or not raw or any(c < 48 or c > 57 for c in raw):
            raise RequestRejected(400, "invalid_request")
        declared = int(raw)
        if declared > max_bytes:
            raise RequestRejected(413, "request_too_large")
    body = bytearray()
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                message = await receive()
                if message.get("type") != "http.request" or type(message.get("body", b"")) is not bytes:
                    raise RequestRejected(400, "invalid_request")
                part = message.get("body", b"")
                if len(part) > max_bytes - len(body):
                    raise RequestRejected(413, "request_too_large")
                body.extend(part)
                if not message.get("more_body", False):
                    break
    except TimeoutError:
        raise RequestRejected(408, "request_timeout") from None
    if declared is not None and declared != len(body):
        raise RequestRejected(400, "invalid_request")
    return bytes(body)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ValueError


def validate_json_payload(body: bytes, max_bytes: int) -> object:
    if len(body) > max_bytes:
        raise RequestRejected(413, "request_too_large")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        validate_domain_input(value, max_bytes=max_bytes)
        return value
    except AIResponseError as error:
        raise RequestRejected(413 if error.code == "input_too_large" else 400, "invalid_request") from None
    except (ValueError, UnicodeError, RecursionError):
        raise RequestRejected(400, "invalid_request") from None


async def send_json(send: Send, status: int, code: str, *, retry_after_seconds: float | None = None,
                    challenge: str | None = None) -> None:
    body = json.dumps({"code": code}, separators=(",", ":")).encode("ascii")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
    if retry_after_seconds is not None and math.isfinite(retry_after_seconds):
        headers.append((b"retry-after", str(max(1, math.ceil(retry_after_seconds))).encode()))
    if challenge == "Basic":
        headers.append((b"www-authenticate", b'Basic realm="TAIP", charset="UTF-8"'))
    elif challenge == "Bearer":
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
