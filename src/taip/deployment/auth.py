"""Bounded single-team authentication and browser request-origin enforcement."""

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib
import hmac

from taip.deployment.settings import (
    DeploymentConfigurationError, _boolean, _digest, _normalize_origin, _secret_token, _username,
)


def _headers_named(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> list[bytes] | None:
    """Malformed input fails closed instead of escaping into a response body."""
    found = []
    try:
        for pair in headers:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                return None
            key, value = pair
            if type(key) is not bytes or type(value) is not bytes:
                return None
            if key.lower() == name:
                found.append(value)
    except (TypeError, ValueError):
        return None
    return found


def _authorization(headers: Sequence[tuple[bytes, bytes]]) -> tuple[bytes, bytes] | None:
    values = _headers_named(headers, b"authorization")
    if values is None or len(values) != 1 or not 1 <= len(values[0]) <= 4096:
        return None
    pieces = values[0].split(b" ", 1)
    if len(pieces) != 2 or not pieces[1] or pieces[0].lower() not in (b"basic", b"bearer"):
        return None
    return pieces[0].lower(), pieces[1]


@dataclass(frozen=True, slots=True)
class CredentialVerifier:
    access_token_digest: bytes = field(repr=False)
    basic_username: str = field(default="taip", kw_only=True)
    allow_basic: bool = field(default=True, kw_only=True)

    def __post_init__(self) -> None:
        _digest(self.access_token_digest)
        _username(self.basic_username, "basic_username")
        _boolean(self.allow_basic, "allow_basic")

    def authenticate(self, headers: Sequence[tuple[bytes, bytes]]) -> str | None:
        authorization = _authorization(headers)
        if authorization is None:
            return None
        scheme, encoded = authorization
        try:
            if scheme == b"bearer":
                token = _secret_token(encoded.decode("ascii"), "authorization")
                digest = hashlib.sha256(token.encode("ascii")).digest()
                return "bearer" if hmac.compare_digest(digest, self.access_token_digest) else None
            if not self.allow_basic:
                return None
            decoded = base64.b64decode(encoded, validate=True)
            if base64.b64encode(decoded) != encoded:
                return None
            credential = decoded.decode("utf8")
            if ":" not in credential:
                return None
            username, token = credential.split(":", 1)
            _secret_token(token, "authorization")
            username_matches = hmac.compare_digest(username.encode("utf8"), self.basic_username.encode("utf8"))
            token_matches = hmac.compare_digest(hashlib.sha256(token.encode("ascii")).digest(), self.access_token_digest)
            return "basic" if username_matches & token_matches else None
        except (DeploymentConfigurationError, UnicodeError, ValueError, binascii.Error):
            return None


def origin_is_allowed(
    method: str, headers: Sequence[tuple[bytes, bytes]], *, scheme: str, expected_origin: str,
    authentication_kind: str | None = None,
) -> bool:
    """Check public TLS and Origin policy after authentication succeeds.

``scheme`` is the actual transport or the configured origin scheme when a
private TLS terminator is explicitly trusted. Forwarding headers are ignored.
The cookie kind is supplied only after the gateway verifies its session.
Originless browser Basic/cookie requests may only navigate with GET/HEAD/OPTIONS;
explicit Bearer credentials also support originless command-line mutations.
"""
    if type(method) is not str or method not in ("GET", "HEAD", "OPTIONS", "POST", "PATCH", "PUT", "DELETE"):
        return False
    if scheme != "https":
        return False
    authorization = _authorization(headers)
    if authentication_kind == "cookie":
        credential_scheme = b"cookie"
    elif authentication_kind is None and authorization is not None:
        credential_scheme = authorization[0]
    else:
        return False
    origins = _headers_named(headers, b"origin")
    if origins is None or len(origins) > 1:
        return False
    try:
        expected = _normalize_origin(expected_origin)
        if origins:
            if len(origins[0]) > 2048:
                return False
            actual = _normalize_origin(origins[0].decode("ascii"))
            return hmac.compare_digest(actual, expected)
    except (DeploymentConfigurationError, UnicodeError, ValueError):
        return False
    return credential_scheme == b"bearer" or method in ("GET", "HEAD", "OPTIONS")
