"""Immutable, fail-closed configuration for the optional protected deployment.

The process environment and secret files are consulted only by the explicit
loader. Secret values are never included in configuration representations or
configuration errors.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit


_TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
# The longest deployment suffix, ':providers', must fit the quota backend's
# 100-character namespace limit as part of the complete configuration.
_NAMESPACE = re.compile(r"taip:[A-Za-z0-9:_-]{1,85}\Z", re.ASCII)
_INTEGER = re.compile(r"[0-9]+\Z", re.ASCII)
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z", re.ASCII)
_DIGEST_HEX = re.compile(r"[A-Fa-f0-9]{64}\Z", re.ASCII)
_MAX_SECRET_BYTES = 4096
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class DeploymentConfigurationError(ValueError):
    """A safe diagnostic identifying a configuration field, never its value."""

    def __init__(self, field: str):
        self.field = field
        self.code = "deployment_configuration_error"
        super().__init__("Invalid deployment configuration.")


def _fail(name: str) -> None:
    raise DeploymentConfigurationError(name) from None


def _trimmed_string(value: object, name: str, *, max_length: int = 4096) -> str:
    if (type(value) is not str or not value or value != value.strip()
            or len(value) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail(name)
    return value


def _username(value: object, name: str) -> str:
    value = _trimmed_string(value, name, max_length=128)
    if ":" in value:
        _fail(name)
    try:
        value.encode("utf8")
    except UnicodeError:
        raise DeploymentConfigurationError(name) from None
    return value


def _secret_token(value: object, name: str, *, minimum_length: int = 43) -> str:
    if (type(value) is not str or not minimum_length <= len(value) <= 256
            or _TOKEN.fullmatch(value) is None):
        _fail(name)
    return value


def _digest(value: object, name: str = "access_token_digest") -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail(name)
    return value


def _positive_integer(value: object, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        _fail(name)
    return value


def _positive_number(value: object, name: str) -> int | float:
    if type(value) not in (int, float):
        _fail(name)
    try:
        valid = value > 0 and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        _fail(name)
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        _fail(name)
    return value


def _host(value: object, name: str) -> str:
    value = _trimmed_string(value, name, max_length=253)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not all(_HOST_LABEL.fullmatch(label) is not None for label in value.split(".")):
            _fail(name)
    if "%" in value:
        _fail(name)
    return value


def _normalize_origin(
    value: object, *, name: str = "public_origin", https_only: bool = True,
    strip_default_port: bool = True,
) -> str:
    """Validate an origin without silently discarding URL payload or controls."""
    value = _trimmed_string(value, name, max_length=2048)
    if not value.isascii() or any(char in value for char in "\\?#"):
        _fail(name)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise DeploymentConfigurationError(name) from None
    schemes = ("https",) if https_only else ("http", "https")
    if (parsed.scheme not in schemes or not parsed.netloc or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or "@" in parsed.netloc or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)):
        _fail(name)
    _host(host, name)
    rendered_host = f"[{host.lower()}]" if ":" in host else host.lower()
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = "" if port is None or (strip_default_port and port == default_port) else f":{port}"
    return f"{parsed.scheme}://{rendered_host}{port_suffix}"


@dataclass(frozen=True, slots=True, kw_only=True)
class RedisSettings:
    host: str = "redis"
    port: int = 6379
    username: str = "taip"
    password: str = field(repr=False)
    namespace: str = "taip:v1"
    tls: bool = False

    def __post_init__(self) -> None:
        _host(self.host, "redis.host")
        _positive_integer(self.port, "redis.port", maximum=65535)
        _username(self.username, "redis.username")
        _secret_token(self.password, "redis.password", minimum_length=32)
        if type(self.namespace) is not str or _NAMESPACE.fullmatch(self.namespace) is None:
            _fail("redis.namespace")
        _boolean(self.tls, "redis.tls")


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    public_origin: str
    access_token_digest: bytes = field(repr=False)
    internal_token: str = field(repr=False)
    redis: RedisSettings
    basic_username: str = "taip"
    core_url: str = "http://core:8000"
    web_url: str = "http://web:80"
    max_body_bytes: int = 1048576
    max_response_bytes: int = 4194304
    body_timeout_seconds: float = 10
    request_timeout_seconds: float = 300
    redis_lease_seconds: float = 900
    gateway_workers: int = 2
    http_requests_per_minute: int = 120
    http_requests_per_day: int = 5000
    http_max_concurrent: int = 16
    live_ai: bool = False
    trust_tls_proxy: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_origin", _normalize_origin(self.public_origin))
        _digest(self.access_token_digest)
        _secret_token(self.internal_token, "internal_token")
        if type(self.redis) is not RedisSettings:
            _fail("redis")
        _username(self.basic_username, "basic_username")
        for name in ("core_url", "web_url"):
            object.__setattr__(self, name, _normalize_origin(
                getattr(self, name), name=name, https_only=False, strip_default_port=False
            ))
        for name in ("max_body_bytes", "max_response_bytes"):
            _positive_integer(getattr(self, name), name, maximum=_MAX_PAYLOAD_BYTES)
        for name in ("gateway_workers", "http_requests_per_minute", "http_requests_per_day", "http_max_concurrent"):
            _positive_integer(getattr(self, name), name)
        for name in ("body_timeout_seconds", "request_timeout_seconds", "redis_lease_seconds"):
            _positive_number(getattr(self, name), name)
        # Connect/read timeouts are two seconds each. Leave ten additional
        # seconds for admission/settlement I/O and scheduling around the two
        # independently bounded HTTP phases.
        protected_seconds = self.body_timeout_seconds + self.request_timeout_seconds + 10
        if protected_seconds >= self.redis_lease_seconds or self.redis_lease_seconds > 86400:
            _fail("redis_lease_seconds")
        _boolean(self.live_ai, "live_ai")
        _boolean(self.trust_tls_proxy, "trust_tls_proxy")
        if hmac.compare_digest(self.access_token_digest, hashlib.sha256(self.internal_token.encode("ascii")).digest()):
            _fail("access_token_digest")
        if hmac.compare_digest(self.redis.password, self.internal_token):
            _fail("redis.password")


def _read_secret_file(path: str) -> str:
    """Bound reads and reject non-regular files before opening special devices."""
    secret_path = Path(path)
    if not stat.S_ISREG(secret_path.stat().st_mode):
        raise OSError("Secret file is unavailable.")
    with secret_path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("Secret file is unavailable.")
        contents = stream.read(_MAX_SECRET_BYTES + 1)
    if len(contents) > _MAX_SECRET_BYTES:
        raise OSError("Secret file is unavailable.")
    return contents.decode("utf8")


def load_deployment_settings(
    environment: Mapping[str, str] | None = None, *, secret_reader: Callable[[str], str] | None = None,
) -> DeploymentSettings:
    """Load public policy and file-backed secrets at startup, with injectable I/O."""
    source = os.environ if environment is None else environment
    reader = _read_secret_file if secret_reader is None else secret_reader

    def text(key: str, name: str, default: str | None = None) -> str:
        return _trimmed_string(source.get(key, default), name)

    def integer(key: str, name: str, default: str) -> int:
        value = text(key, name, default)
        if len(value) > 20 or _INTEGER.fullmatch(value) is None:
            _fail(name)
        return int(value)

    def number(key: str, name: str, default: str) -> float:
        value = text(key, name, default)
        if len(value) > 64 or _NUMBER.fullmatch(value) is None:
            _fail(name)
        return float(value)

    def boolean(key: str, name: str, default: str = "false") -> bool:
        value = text(key, name, default)
        if value not in ("true", "false"):
            _fail(name)
        return value == "true"

    def secret(key: str, name: str) -> str:
        path = text(key, name)
        try:
            value = reader(path)
        except (OSError, UnicodeError):
            raise DeploymentConfigurationError(name) from None
        if type(value) is not str:
            _fail(name)
        try:
            bounded = len(value.encode("utf8")) <= _MAX_SECRET_BYTES
        except UnicodeError:
            raise DeploymentConfigurationError(name) from None
        if not bounded:
            _fail(name)
        return value.rstrip("\r\n")

    public_origin = text("TAIP_PUBLIC_ORIGIN", "public_origin")
    digest_hex = secret("TAIP_ACCESS_TOKEN_SHA256_FILE", "access_token_digest")
    if _DIGEST_HEX.fullmatch(digest_hex) is None:
        _fail("access_token_digest")
    internal_token = secret("TAIP_INTERNAL_TOKEN_FILE", "internal_token")
    redis = RedisSettings(
        host=text("TAIP_REDIS_HOST", "redis.host", "redis"),
        port=integer("TAIP_REDIS_PORT", "redis.port", "6379"),
        username=text("TAIP_REDIS_USERNAME", "redis.username", "taip"),
        password=secret("TAIP_REDIS_PASSWORD_FILE", "redis.password"),
        namespace=text("TAIP_REDIS_NAMESPACE", "redis.namespace", "taip:v1"),
        tls=boolean("TAIP_REDIS_TLS", "redis.tls"),
    )
    return DeploymentSettings(
        public_origin=public_origin, access_token_digest=bytes.fromhex(digest_hex),
        internal_token=internal_token, redis=redis,
        basic_username=text("TAIP_API_USERNAME", "basic_username", "taip"),
        core_url=text("TAIP_CORE_URL", "core_url", "http://core:8000"),
        web_url=text("TAIP_WEB_URL", "web_url", "http://web:80"),
        max_body_bytes=integer("TAIP_MAX_BODY_BYTES", "max_body_bytes", "1048576"),
        max_response_bytes=integer("TAIP_MAX_RESPONSE_BYTES", "max_response_bytes", "4194304"),
        body_timeout_seconds=number("TAIP_BODY_TIMEOUT_SECONDS", "body_timeout_seconds", "10"),
        request_timeout_seconds=number("TAIP_REQUEST_TIMEOUT_SECONDS", "request_timeout_seconds", "300"),
        redis_lease_seconds=number("TAIP_REDIS_LEASE_SECONDS", "redis_lease_seconds", "900"),
        gateway_workers=integer("TAIP_GATEWAY_WORKERS", "gateway_workers", "2"),
        http_requests_per_minute=integer("TAIP_HTTP_REQUESTS_PER_MINUTE", "http_requests_per_minute", "120"),
        http_requests_per_day=integer("TAIP_HTTP_REQUESTS_PER_DAY", "http_requests_per_day", "5000"),
        http_max_concurrent=integer("TAIP_HTTP_MAX_CONCURRENT", "http_max_concurrent", "16"),
        live_ai=boolean("TAIP_LIVE_AI", "live_ai"),
        trust_tls_proxy=boolean("TAIP_TLS_TERMINATED_BY_PROXY", "trust_tls_proxy"),
    )
