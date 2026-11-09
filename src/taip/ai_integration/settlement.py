"""Bounded process quarantine for ambiguous, idempotent quota completion.

Only permit identities and accounting numbers are retained. A paid provider
response never enters this registry, and recovery never repeats provider HTTP.
Redis remains authoritative across processes; its original request charges and
conservative active reservations are not refunded by this process quarantine.
"""

from collections.abc import Hashable
from dataclasses import dataclass, field
from threading import Lock, RLock

from .errors import AIProviderError
from .rate_limits import RateLimitPermitPort, RateLimitPolicy, RateLimitPort


class SettlementPendingError(AIProviderError):
    """The preceding attempt's completion is still unacknowledged."""

    def __init__(self) -> None:
        super().__init__("admission_unavailable", retry_after_seconds=1)


@dataclass(slots=True, eq=False, repr=False)
class _CredentialState:
    lock: RLock = field(default_factory=RLock)
    users: int = 0
    busy: bool = False
    records: set["TrackedSettlement"] = field(default_factory=set)


@dataclass(slots=True, eq=False, repr=False)
class TrackedSettlement:
    """Reserve recovery capacity before HTTP; report completion separately."""

    coordinator: "SettlementCoordinator"
    key: Hashable
    state: _CredentialState
    permit: RateLimitPermitPort
    pending: bool = False
    actual_tokens: int | None = None
    cooldown_seconds: float | None = None

    def finish(
        self, *, actual_tokens: int | None = None, cooldown_seconds: float | None = None,
    ) -> bool:
        return self.coordinator._finish(self, actual_tokens, cooldown_seconds)


class SettlementCoordinator:
    """Share quarantine across aliases, factories, and gates in this process.

    The cap includes active attempts so all simultaneous completion failures
    can be retained. Pending entries have no expiry: forgetting one would
    reopen uncertain admission. Clean entries are removed immediately. A
    credential lock covers reconciliation and admission, never provider HTTP;
    the registry lock never covers backend I/O or observer callbacks.
    """

    def __init__(self, *, max_records: int = 1024) -> None:
        if type(max_records) is not int or max_records <= 0:
            raise ValueError("The settlement capacity must be a positive integer.")
        self._capacity = max_records
        self._lock = Lock()
        self._states: dict[Hashable, _CredentialState] = {}
        self._records = 0

    def acquire(
        self, limiter: RateLimitPort, key: Hashable, *, policy: RateLimitPolicy,
        estimated_tokens: int, provider: str, model_id: str,
    ) -> TrackedSettlement:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= self._capacity:
                    raise AIProviderError("admission_unavailable", retry_after_seconds=1)
                state = _CredentialState()
                self._states[key] = state
            # Never wait for a credential doing backend I/O. In particular,
            # observers on two threads cannot form cross-credential lock cycles.
            if not state.lock.acquire(blocking=False):
                raise SettlementPendingError()
            state.users += 1
        try:
            try:
                # Backend observers may re-enter on this same thread. RLock
                # avoids deadlock; busy rejects that admission conservatively.
                if state.busy:
                    raise SettlementPendingError()
                state.busy = True
                try:
                    for record in tuple(state.records):
                        if record.pending and not self._try_finish(record):
                            raise SettlementPendingError() from None
                    with self._lock:
                        if self._records >= self._capacity:
                            raise AIProviderError("admission_unavailable", retry_after_seconds=1)
                        self._records += 1
                    try:
                        permit = limiter.acquire(
                            key, policy=policy, estimated_tokens=estimated_tokens,
                            provider=provider, model_id=model_id,
                        )
                    except BaseException:
                        with self._lock:
                            self._records -= 1
                        raise
                    record = TrackedSettlement(self, key, state, permit)
                    state.records.add(record)
                    return record
                finally:
                    state.busy = False
            finally:
                with self._lock:
                    state.users -= 1
                    self._remove_idle(key, state)
        finally:
            state.lock.release()

    def _finish(
        self, record: TrackedSettlement, actual_tokens: int | None,
        cooldown_seconds: float | None,
    ) -> bool:
        state = record.state
        with state.lock:
            if record not in state.records:
                return True
            record.actual_tokens = actual_tokens
            record.cooldown_seconds = cooldown_seconds
            record.pending = True
            if state.busy:
                return False
            state.busy = True
            try:
                return self._try_finish(record)
            finally:
                state.busy = False
                with self._lock:
                    self._remove_idle(record.key, state)

    def _try_finish(self, record: TrackedSettlement) -> bool:
        try:
            record.permit.finish(
                actual_tokens=record.actual_tokens, cooldown_seconds=record.cooldown_seconds,
            )
        except Exception:
            # Retain neither exceptions (which may contain URLs/secrets) nor
            # response bodies. Retry this same idempotent permit only on the
            # next explicit admission attempt for this credential.
            return False
        record.state.records.remove(record)
        with self._lock:
            self._records -= 1
        return True

    def _remove_idle(self, key: Hashable, state: _CredentialState) -> None:
        # Caller holds both this credential's lock and the registry lock.
        if state.users == 0 and not state.records:
            del self._states[key]


# Adapter/factory recreation cannot bypass an unresolved credential within the
# worker. This registry contains no provider response or application session.
settlements = SettlementCoordinator()
