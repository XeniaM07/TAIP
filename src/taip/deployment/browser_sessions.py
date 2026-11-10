"""Optional gateway browser credentials with bounded, shared Redis authority.

Only digests reach Redis. Codes are single-use and sessions have a fixed
absolute expiry; authentication never renews either lifetime. The caller owns
the existing Redis client and runs these synchronous operations off its loop.
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import re
import secrets
from typing import Protocol

from taip.deployment.settings import (
    DeploymentConfigurationError, _NAMESPACE, _digest, _normalize_origin,
)


CODE_TTL_SECONDS = 120
SESSION_TTL_SECONDS = 43200
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_COOKIE_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}\Z", re.ASCII)
_COOKIE_VALUE = re.compile(rb"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*\Z", re.ASCII)
_MAX_COOKIE_BYTES = 8192

# Both keys have the same cluster hash tag. Every command is already granted
# by setup_public's ACL, including the atomic GETDEL equivalent (ZSCORE/ZREM).
# Validate bounded existing state before any mutation so corrupt types,
# oversized sets, or forged future expiry cannot silently regain authority.
_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local code_ttl = tonumber(ARGV[4])
local session_ttl = tonumber(ARGV[5])
local function valid_set(key, capacity, ttl)
    if redis.call('ZCARD', key) > capacity then return false end
    local values = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
    for index = 1, #values, 2 do
        local member = values[index]
        local expiry = tonumber(values[index + 1])
        if #member ~= 64 or not string.match(member, '^[0-9a-f]+$')
                or not expiry or expiry <= 0 or expiry ~= math.floor(expiry)
                or expiry > now + ttl then
            return false
        end
    end
    return true
end
if not valid_set(KEYS[1], 8, code_ttl)
        or not valid_set(KEYS[2], 128, session_ttl) then return -1 end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local function add_bounded(key, member, capacity, ttl)
    if redis.call('ZCARD', key) >= capacity then
        local oldest = redis.call('ZRANGE', key, 0, 0)
        redis.call('ZREM', key, oldest[1])
    end
    redis.call('ZADD', key, now + ttl, member)
    redis.call('PEXPIRE', key, ttl)
end
if ARGV[1] == 'mint' then
    if redis.call('ZSCORE', KEYS[1], ARGV[2]) then return -2 end
    add_bounded(KEYS[1], ARGV[2], 8, code_ttl)
    return 1
elseif ARGV[1] == 'exchange' then
    if not redis.call('ZSCORE', KEYS[1], ARGV[2]) then return 0 end
    if redis.call('ZSCORE', KEYS[2], ARGV[3]) then return -2 end
    redis.call('ZREM', KEYS[1], ARGV[2])
    add_bounded(KEYS[2], ARGV[3], 128, session_ttl)
    return 1
elseif ARGV[1] == 'authenticate' then
    if redis.call('ZSCORE', KEYS[2], ARGV[2]) then return 1 end
    return 0
end
return -1
"""


class BrowserSessionUnavailable(RuntimeError):
    """Safe failure without Redis addresses, credentials, or token values."""

    def __init__(self) -> None:
        super().__init__("Browser login is unavailable.")


class BrowserSessionPort(Protocol):
    @property
    def cookie_name(self) -> str: ...

    def mint(self) -> str: ...

    def exchange(self, code: str) -> str | None: ...

    def authenticate(self, value: str) -> bool: ...


def _valid_token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _new_token() -> str:
    try:
        value = secrets.token_urlsafe(32)
    except Exception:
        raise BrowserSessionUnavailable() from None
    if not _valid_token(value):
        raise BrowserSessionUnavailable()
    return value


class RedisBrowserSessions:
    """Two fixed-size sorted sets shared by all gateway workers.

    Redis TIME supplies absolute millisecond deadlines. The oldest expiry is
    evicted at capacity (equal deadlines are equally old). Binding the keys to
    both origin and application credential makes rotation revoke old sessions
    without a key scan, ACL migration, or interruption of the workflow core.
    """

    def __init__(self, client, *, namespace: str, public_origin: str,
                 access_token_digest: bytes) -> None:
        if type(namespace) is not str or _NAMESPACE.fullmatch(namespace) is None:
            raise DeploymentConfigurationError("browser_sessions.namespace")
        origin = _normalize_origin(public_origin)
        _digest(access_token_digest)
        binding = hashlib.sha256(
            namespace.encode("ascii") + b"\0" + origin.encode("ascii") + b"\0" + access_token_digest,
        ).hexdigest()
        prefix = namespace + ":browser:{" + binding + "}:"
        self._keys = (prefix + "codes", prefix + "sessions")
        self._cookie_name = "__Host-taip_session_" + hashlib.sha256(origin.encode("ascii")).hexdigest()[:16]
        self._client = client

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    def _run(self, operation: str, digest: str, session_digest: str = "") -> int:
        try:
            result = self._client.eval(_SCRIPT, 2, *self._keys, operation, digest, session_digest,
                                       CODE_TTL_SECONDS * 1000, SESSION_TTL_SECONDS * 1000)
        except Exception:
            raise BrowserSessionUnavailable() from None
        if type(result) is not int or result not in (-2, 0, 1):
            raise BrowserSessionUnavailable()
        return result

    def mint(self) -> str:
        for _ in range(3):
            code = _new_token()
            result = self._run("mint", _token_digest(code))
            if result == 1:
                return code
            if result != -2:
                raise BrowserSessionUnavailable()
        raise BrowserSessionUnavailable()

    def exchange(self, code: str) -> str | None:
        if not _valid_token(code):
            return None
        digest = _token_digest(code)
        for _ in range(3):
            session = _new_token()
            if session == code:
                continue
            result = self._run("exchange", digest, _token_digest(session))
            if result == 1:
                return session
            if result == 0:
                return None
        raise BrowserSessionUnavailable()

    def authenticate(self, value: str) -> bool:
        if not _valid_token(value):
            return False
        result = self._run("authenticate", _token_digest(value))
        if result == -2:
            raise BrowserSessionUnavailable()
        return result == 1


def read_browser_cookie(headers: Sequence[tuple[bytes, bytes]], cookie_name: str) -> str | None:
    """Read one exact opaque cookie; malformed or ambiguous input fails closed."""
    try:
        target = cookie_name.encode("ascii") if type(cookie_name) is str else b""
        if _COOKIE_NAME.fullmatch(target) is None or not isinstance(headers, (list, tuple)) or len(headers) > 100:
            return None
        found = None
        header_bytes = 0
        cookie_bytes = 0
        for pair in headers:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return None
            name, value = pair
            if type(name) is not bytes or type(value) is not bytes:
                return None
            header_bytes += len(name) + len(value)
            if header_bytes > 16384:
                return None
            if name.lower() != b"cookie":
                continue
            cookie_bytes += len(value)
            if cookie_bytes > _MAX_COOKIE_BYTES:
                return None
            for segment in value.split(b";"):
                name, separator, value = segment.lstrip(b" ").partition(b"=")
                if not separator or _COOKIE_NAME.fullmatch(name) is None:
                    return None
                content = value[1:-1] if len(value) >= 2 and value.startswith(b'"') and value.endswith(b'"') else value
                if _COOKIE_VALUE.fullmatch(content) is None:
                    return None
                if name == target:
                    if found is not None:
                        return None
                    candidate = value.decode("ascii")
                    if not _valid_token(candidate):
                        return None
                    found = candidate
        return found
    except (UnicodeError, TypeError, ValueError):
        return None
