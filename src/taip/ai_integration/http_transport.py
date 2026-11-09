"""Reusable synchronous JSON transport with explicit, bounded HTTP behavior."""

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from types import TracebackType
from typing import Protocol

import httpx

from taip.ai_integration.errors import AIProviderError, provider_error_for_status


class JSONTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, object]: ...


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            seconds = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _decode_envelope(content: bytes) -> dict[str, object]:
    try:
        result = json.loads(
            content.decode("utf-8"), object_pairs_hook=_json_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (ValueError, RecursionError):
        raise AIProviderError("invalid_envelope") from None
    if not isinstance(result, dict):
        raise AIProviderError("invalid_envelope")
    return result


class HTTPJSONTransport:
    """Own one connection pool; never retry, redirect, or use ambient proxies.

    Timeout values apply to HTTPX connect/read/write/pool inactivity, rather
    than a total wall-clock deadline. Identity encoding avoids an unbounded
    decompression allocation before the response byte limit can be checked.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            transport=transport if transport is not None else httpx.HTTPTransport(
                retries=0, trust_env=False,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, object]:
        request_headers = httpx.Headers(headers)
        request_headers["Accept-Encoding"] = "identity"
        try:
            with self._client.stream(
                "POST", url, headers=request_headers, json=dict(body),
                timeout=timeout_seconds, follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code <= 299:
                    raise provider_error_for_status(
                        response.status_code,
                        retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
                    )
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise AIProviderError("unsupported_encoding")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > max_response_bytes:
                        raise AIProviderError("response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise AIProviderError("timeout") from None
        except httpx.RequestError:
            raise AIProviderError("network_error") from None
        return _decode_envelope(bytes(content))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HTTPJSONTransport":
        return self

    def __exit__(
        self, exception_type: type[BaseException] | None,
        exception: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        self.close()
