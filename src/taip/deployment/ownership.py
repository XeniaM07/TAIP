"""A renewable Redis guard against two independent in-memory workflow cores."""

import hmac
import math
from threading import Event, Lock, Thread, current_thread
from uuid import uuid4

from taip.deployment.settings import _NAMESPACE, _positive_number, DeploymentConfigurationError


_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class CoreOwnershipLease:
    """Own one namespace until shutdown or proven lease loss, without recovery.

    Loss permanently fences this object. A fresh process must create a fresh
    workflow and lease; an old in-memory history must never be resurrected.
    The synchronous Redis client remains owned by its caller.
    """

    def __init__(self, client, *, namespace: str, lease_seconds: float = 30):
        if type(namespace) is not str or _NAMESPACE.fullmatch(namespace) is None:
            raise DeploymentConfigurationError("redis.namespace")
        _positive_number(lease_seconds, "core_lease_seconds")
        milliseconds = lease_seconds * 1000
        if not math.isfinite(milliseconds) or milliseconds > 2**53 - 1:
            raise DeploymentConfigurationError("core_lease_seconds")
        self._client = client
        self._key = namespace + ":core-owner"
        self._token = uuid4().hex.encode("ascii")
        self._lease_ms = max(1, math.ceil(milliseconds))
        self._interval = lease_seconds / 3
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._started = False
        self._lost = False
        self._closed = False

    @staticmethod
    def _startup_error():
        return RuntimeError("Core ownership is unavailable; restart is required.")

    def start(self) -> None:
        with self._lock:
            if self._closed or self._lost:
                raise self._startup_error()
            if self._started:
                if not self._check_current():
                    raise self._startup_error()
                return
            try:
                admitted = self._client.set(self._key, self._token, nx=True, px=self._lease_ms)
            except Exception:
                self._mark_lost()
                raise self._startup_error() from None
            if not admitted:
                self._mark_lost()
                raise self._startup_error()
            self._started = True
            self._thread = Thread(target=self._heartbeat, name="taip-core-ownership", daemon=True)
            self._thread.start()

    def _mark_lost(self):
        self._lost = True
        self._stop.set()

    def _check_current(self):
        try:
            current = self._client.get(self._key)
            if type(current) is str:
                current = current.encode("ascii")
            matches = type(current) is bytes and hmac.compare_digest(current, self._token)
        except Exception:
            matches = False
        if not matches:
            self._mark_lost()
        return matches

    def is_current(self) -> bool:
        with self._lock:
            if not self._started or self._closed or self._lost:
                return False
            return self._check_current()

    def _heartbeat(self):
        while not self._stop.wait(self._interval):
            with self._lock:
                if self._closed or self._lost:
                    return
                try:
                    renewed = self._client.eval(_RENEW, 1, self._key, self._token, self._lease_ms)
                except Exception:
                    renewed = 0
                if renewed != 1:
                    self._mark_lost()
                    return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            thread = self._thread
            started = self._started
        if thread is not None and thread is not current_thread():
            thread.join(timeout=5)
        if started:
            try:
                self._client.eval(_RELEASE, 1, self._key, self._token)
            except Exception:
                pass
