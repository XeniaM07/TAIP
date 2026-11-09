"""Redis-backed preventive admission, with no credentials or session storage.

The caller owns the synchronous Redis client and its connection pool. Server
TIME, rather than worker clocks, controls every rolling window and lease. An
admitted operation must finish within ``lease_seconds``; expired permits cannot
be revived. The deployment must prohibit eviction of these quota keys.
"""

from collections.abc import Hashable
from fractions import Fraction
from hashlib import sha256
from importlib.resources import files
import json
import logging
import math
import re
from threading import local
from typing import Any
from uuid import uuid4

from taip.ai_integration.errors import AIConfigurationError, AIProviderError
from taip.ai_integration.rate_limits import (
    QuotaEvent, QuotaObserver, QuotaSnapshot, RateLimitPolicy,
)


_LOGGER = logging.getLogger(__name__)
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,99}\Z")
_METADATA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_MAX_INTEGER = 2**53 - 1
_DIMENSIONS = {"requests_per_minute", "requests_per_day", "tokens_per_minute",
               "max_concurrent", "cooldown"}
_SUFFIXES = ("policy", "minute", "day", "active", "reservations", "settled",
             "tokens", "cooldown", "warned", "blocked", "records")


def _unavailable() -> AIProviderError:
    return AIProviderError("admission_unavailable", retry_after_seconds=1)


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= _MAX_INTEGER


def _wire_integer(value: Any) -> int:
    # Redis cjson defaults to fourteen significant digits. Lua therefore sends
    # integer counters as canonical decimal strings, preserving all 53 bits.
    if type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,15}", value):
        value = int(value)
    if not _integer(value):
        raise ValueError
    return value


def _encode_key(key: Hashable) -> bytes:
    """Length and type framing prevents collisions between strings and bytes."""
    def scalar(value: Any) -> bytes:
        if type(value) is str:
            raw = value.encode("utf-8")
            tag = b"s"
        elif type(value) is bytes:
            raw = value
            tag = b"b"
        else:
            raise AIConfigurationError("rate_limits")
        return tag + len(raw).to_bytes(8, "big") + raw

    try:
        if type(key) is tuple:
            return b"t" + len(key).to_bytes(8, "big") + b"".join(scalar(part) for part in key)
        return scalar(key)
    except (UnicodeError, OverflowError):
        raise AIConfigurationError("rate_limits") from None


class RedisRateLimitPermit:
    """A replay-safe distributed reservation; successful finish is idempotent."""

    __slots__ = ("_limiter", "_keys", "_reservation_id")

    def __init__(self, limiter: "RedisRateLimiter", keys: tuple[str, ...], reservation_id: str):
        self._limiter = limiter
        self._keys = keys
        self._reservation_id = reservation_id

    def finish(
        self, *, actual_tokens: int | None = None, cooldown_seconds: float | None = None,
    ) -> None:
        self._limiter._finish(self._keys, self._reservation_id,
                              actual_tokens=actual_tokens, cooldown_seconds=cooldown_seconds)


class RedisRateLimiter:
    """Atomic rolling quotas shared by processes using the same Redis database.

    ``acquisition_id`` is optional on acquire. A caller recovering an ambiguous
    Redis response may replay the same ID and arguments without a second debit.
    IDs remain deduplicated for a rolling day plus the lease; they must identify
    one logical admission, never an additional provider invocation. This class
    itself never retries Redis commands or provider HTTP requests.
    """

    def __init__(
        self, client: Any, *, namespace: str = "taip:v1:providers",
        lease_seconds: float = 900, observer: QuotaObserver | None = None,
    ) -> None:
        if type(namespace) is not str or _NAMESPACE.fullmatch(namespace) is None:
            raise AIConfigurationError("rate_limits")
        if (type(lease_seconds) not in (int, float) or not 0 < lease_seconds <= 86400
                or not math.isfinite(lease_seconds)):
            raise AIConfigurationError("rate_limits")
        self._client = client
        self._namespace = namespace
        self._lease_ms = math.ceil(lease_seconds * 1000)
        self._observer = observer
        self._emission = local()
        self._script = files("taip.deployment").joinpath("quota.lua").read_text(encoding="utf-8")

    def _keys(self, key: Hashable) -> tuple[str, ...]:
        digest = sha256(_encode_key(key)).hexdigest()
        # All script keys are declared explicitly and have the same hash tag.
        prefix = f"{self._namespace}:{{{digest}}}:"
        return tuple(prefix + suffix for suffix in _SUFFIXES)

    def acquire(
        self, key: Hashable, *, policy: RateLimitPolicy, estimated_tokens: int,
        provider: str, model_id: str, acquisition_id: str | None = None,
    ) -> RedisRateLimitPermit:
        if not isinstance(policy, RateLimitPolicy):
            raise AIConfigurationError("rate_limits")
        if not _integer(estimated_tokens):
            raise AIConfigurationError("estimated_tokens")
        for value, field in ((provider, "provider"), (model_id, "model_id")):
            if type(value) is not str or _METADATA.fullmatch(value) is None:
                raise AIConfigurationError(field)
        if acquisition_id is None:
            acquisition_id = uuid4().hex
        if type(acquisition_id) is not str or not 1 <= len(acquisition_id) <= 128:
            raise AIConfigurationError("rate_limits")
        try:
            operation_id = sha256(acquisition_id.encode("utf-8")).hexdigest()
        except UnicodeError:
            raise AIConfigurationError("rate_limits") from None
        limits = (policy.requests_per_minute, policy.requests_per_day,
                  policy.tokens_per_minute, policy.max_concurrent)
        if not all(_integer(limit, minimum=1) for limit in limits):
            raise AIConfigurationError("rate_limits")
        fraction = Fraction(str(policy.warning_fraction))
        thresholds = tuple(math.ceil(limit * fraction) for limit in limits)
        binding = json.dumps([*limits, *thresholds, str(fraction), self._lease_ms],
                             separators=(",", ":"))
        keys = self._keys(key)
        result = self._execute(keys, "acquire", binding, operation_id,
                               str(estimated_tokens), provider, model_id)
        if result["status"] == "conflict":
            raise AIConfigurationError("rate_limits")
        if result["status"] == "denied":
            raise AIProviderError(result["code"], retry_after_seconds=result["retry_after_seconds"])
        if result["status"] != "admitted":
            raise _unavailable() from None
        return RedisRateLimitPermit(self, keys, operation_id)

    def snapshot(self, key: Hashable) -> QuotaSnapshot:
        result = self._execute(self._keys(key), "snapshot")
        if result["status"] != "snapshot":
            raise _unavailable() from None
        return result["snapshot"]

    def _finish(
        self, keys: tuple[str, ...], reservation_id: str, *, actual_tokens: int | None,
        cooldown_seconds: float | None,
    ) -> None:
        actual = str(actual_tokens) if _integer(actual_tokens) else ""
        duration = ""
        if type(cooldown_seconds) in (int, float):
            try:
                if math.isfinite(cooldown_seconds) and 0 <= cooldown_seconds <= _MAX_INTEGER / 1000:
                    duration = str(math.ceil(cooldown_seconds * 1000))
            except OverflowError:
                pass
        result = self._execute(keys, "finish", "", reservation_id, actual, duration)
        if result["status"] != "finished":
            raise _unavailable() from None

    def _execute(self, keys: tuple[str, ...], operation: str, *arguments: str) -> dict[str, Any]:
        try:
            raw = self._client.eval(self._script, len(keys), *keys, operation, *arguments)
            result = self._decode(raw, operation)
        except Exception:
            # Never propagate backend connection strings, raw keys or protocol text.
            raise _unavailable() from None
        self._emit(result.pop("events"))
        return result

    @staticmethod
    def _decode(raw: Any, operation: str) -> dict[str, Any]:
        if type(raw) not in (str, bytes):
            raise ValueError
        result = json.loads(raw)
        fields = {"version", "status", "code", "retry_after_seconds", "snapshot", "events"}
        if (type(result) is not dict or set(result) != fields
                or type(result["version"]) is not int or result["version"] != 1):
            raise ValueError
        status = result.get("status")
        allowed = {"acquire": {"admitted", "denied", "conflict", "unavailable"},
                   "finish": {"finished", "unavailable"}, "snapshot": {"snapshot", "unavailable"}}
        if status not in allowed[operation]:
            raise ValueError
        data = result.get("snapshot")
        if type(data) is not dict or set(data) != {
            "requests_in_minute", "requests_in_day", "tokens_in_minute", "in_flight", "cooldown_seconds",
        }:
            raise ValueError
        counts = tuple(_wire_integer(data[name]) for name in (
            "requests_in_minute", "requests_in_day", "tokens_in_minute", "in_flight",
        ))
        cooldown = data.get("cooldown_seconds")
        if (not all(_integer(value) for value in counts) or type(cooldown) not in (int, float)
                or not math.isfinite(cooldown) or cooldown < 0):
            raise ValueError
        result["snapshot"] = QuotaSnapshot(*counts, float(cooldown))
        raw_events = result.get("events")
        if raw_events == {}:  # Redis Lua cjson represents an empty table as {}.
            raw_events = []
        if type(raw_events) is not list or len(raw_events) > 6:
            raise ValueError
        events = []
        for item in raw_events:
            if type(item) is not dict or set(item) != {
                "provider", "model_id", "kind", "dimension", "used", "limit", "log", "retry_after_seconds",
            }:
                raise ValueError
            provider, model = item.get("provider"), item.get("model_id")
            if (type(provider) is not str or _METADATA.fullmatch(provider) is None
                    or type(model) is not str or _METADATA.fullmatch(model) is None
                    or item.get("kind") not in {"near_limit", "blocked", "cooldown"}
                    or item.get("dimension") not in _DIMENSIONS
                    or type(item.get("log")) is not bool):
                raise ValueError
            used, limit = _wire_integer(item["used"]), _wire_integer(item["limit"])
            retry = item.get("retry_after_seconds")
            if retry is not None and (type(retry) not in (int, float)
                                      or not math.isfinite(retry) or retry < 0):
                raise ValueError
            events.append((QuotaEvent(provider, model, item["kind"], item["dimension"],
                                      used, limit, retry), item["log"]))
        result["events"] = events
        if status == "denied":
            retry = result.get("retry_after_seconds")
            if result.get("code") == "local_rate_limit":
                if type(retry) not in (int, float) or not math.isfinite(retry) or retry <= 0:
                    raise ValueError
            elif result.get("code") != "request_budget_exceeded" or retry is not None:
                raise ValueError
        elif result["code"] is not None or result["retry_after_seconds"] is not None:
            raise ValueError
        return result

    def _emit(self, events: list[tuple[QuotaEvent, bool]]) -> None:
        if getattr(self._emission, "active", False):
            return
        self._emission.active = True
        try:
            for event, should_log in events:
                if should_log:
                    try:
                        _LOGGER.warning(
                            "AI quota %s: provider=%s model=%s %s=%s/%s retry_after_seconds=%s",
                            event.kind, event.provider, event.model_id, event.dimension,
                            event.used, event.limit, event.retry_after_seconds,
                            extra={"quota_event": "ai.quota", "quota_provider": event.provider,
                                   "quota_model_id": event.model_id, "quota_kind": event.kind,
                                   "quota_dimension": event.dimension, "quota_used": event.used,
                                   "quota_limit": event.limit,
                                   "quota_retry_after_seconds": event.retry_after_seconds},
                        )
                    except Exception:
                        pass
                if self._observer is not None:
                    try:
                        self._observer(event)
                    except Exception:
                        pass
        finally:
            self._emission.active = False
