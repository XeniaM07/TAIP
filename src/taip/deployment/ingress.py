"""Constant-space, short ingress protection before credential verification.

Public traffic shares one sixty-second counter, never authenticated daily
quota or attacker-selected keys. Redis server time controls the shared window.
"""

from collections.abc import Callable
import re
from threading import Lock
from time import monotonic
from typing import Protocol

from taip.ai_integration.errors import AIConfigurationError, AIProviderError


_MAX_REQUESTS = 1000000
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,99}\Z", re.ASCII)
_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local raw_count = redis.call('HGET', KEYS[1], 'count')
local raw_bound = redis.call('HGET', KEYS[1], 'limit')
local raw_until = redis.call('HGET', KEYS[1], 'until')
local count = tonumber(raw_count)
local bound = tonumber(raw_bound)
local until_ms = tonumber(raw_until)
local limit = tonumber(ARGV[1])
if (not raw_count and not raw_bound and not raw_until) or (until_ms and until_ms <= now) then
    redis.call('HSET', KEYS[1], 'count', 1, 'limit', limit, 'until', now + 60000)
    redis.call('PEXPIRE', KEYS[1], 60000)
    return {1, 0}
end
if not count or count < 1 or count > limit or count ~= math.floor(count)
        or bound ~= limit or not until_ms or until_ms ~= math.floor(until_ms)
        or until_ms - now > 60000 then
    return {-1, 0}
end
if count >= limit then
    return {0, until_ms - now}
end
redis.call('HSET', KEYS[1], 'count', count + 1)
return {1, 0}
"""


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REQUESTS:
        raise AIConfigurationError("http_ingress_requests_per_minute")
    return value


class IngressPort(Protocol):
    def check(self) -> None:
        """Charge one short ingress attempt or reject without extending the window."""
        ...


class LocalIngressLimiter:
    """A one-counter substitute for explicitly injected, process-local deployments."""

    def __init__(self, *, requests_per_minute: int = 600,
                 clock: Callable[[], float] = monotonic) -> None:
        self._limit = _limit(requests_per_minute)
        self._clock = clock
        self._lock = Lock()
        self._used = 0
        self._until = 0.0

    def check(self) -> None:
        with self._lock:
            now = self._clock()
            if now >= self._until:
                self._used = 0
                self._until = now + 60
            if self._used >= self._limit:
                raise AIProviderError("local_rate_limit", retry_after_seconds=self._until - now)
            self._used += 1


class RedisIngressLimiter:
    """One expiring hash shared by all gateway workers, with no request history.

    Denials never mutate the counter or its TTL. The client owns connections
    and must disable automatic retries, as for authenticated admission.
    """

    def __init__(self, client, *, namespace: str = "taip:v1",
                 requests_per_minute: int = 600) -> None:
        self._limit = _limit(requests_per_minute)
        if type(namespace) is not str or _NAMESPACE.fullmatch(namespace) is None:
            raise AIConfigurationError("rate_limits")
        self._client = client
        self._key = namespace + ":ingress"

    def check(self) -> None:
        try:
            result = self._client.eval(_SCRIPT, 1, self._key, self._limit)
            if (type(result) is not list or len(result) != 2
                    or any(type(value) is not int for value in result)):
                raise ValueError
            status, remaining_ms = result
            if status == 1 and remaining_ms == 0:
                return
            if status == 0 and 1 <= remaining_ms <= 60000:
                denied_seconds = remaining_ms / 1000
            else:
                raise ValueError
        except Exception:
            raise AIProviderError("admission_unavailable", retry_after_seconds=1) from None
        raise AIProviderError("local_rate_limit", retry_after_seconds=denied_seconds)
