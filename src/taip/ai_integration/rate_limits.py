"""Process-local preventive quotas for explicitly configured provider budgets.

An application should inject the same limiter into every provider factory that
shares credentials. Separate worker processes require a distributed admission
gate outside this module. Vendor quotas are not discovered automatically.
Only counters, timestamps and opaque credential keys are retained; no prompts,
responses, credentials in plaintext, or application sessions belong here.
"""

from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from fractions import Fraction
import heapq
import logging
import math
from threading import Lock, local
from time import monotonic
from typing import Protocol

from .errors import AIConfigurationError, AIProviderError


_LOGGER = logging.getLogger(__name__)
_MINUTE = 60.0
_DAY = 86400.0


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Conservative application budgets, independent of a vendor's live quota."""

    requests_per_minute: int = 6
    requests_per_day: int = 100
    tokens_per_minute: int = 60000
    max_concurrent: int = 2
    warning_fraction: float = 0.8

    def __post_init__(self) -> None:
        for name in ("requests_per_minute", "requests_per_day", "tokens_per_minute", "max_concurrent"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise AIConfigurationError(name)
        fraction = self.warning_fraction
        if (type(fraction) not in (int, float) or not 0 < fraction <= 1
                or not math.isfinite(fraction)):
            raise AIConfigurationError("warning_fraction")


@dataclass(frozen=True, slots=True)
class QuotaEvent:
    """Safe operational metadata; never include an opaque quota key."""

    provider: str
    model_id: str
    kind: str
    dimension: str
    used: int
    limit: int
    retry_after_seconds: float | None = None


QuotaObserver = Callable[[QuotaEvent], None]


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    requests_in_minute: int
    requests_in_day: int
    tokens_in_minute: int
    in_flight: int
    cooldown_seconds: float


class RateLimitPermitPort(Protocol):
    """A local or distributed admission reservation with explicit completion."""

    def finish(
        self, *, actual_tokens: int | None = None, cooldown_seconds: float | None = None,
    ) -> None: ...


class RateLimitPort(Protocol):
    """Admission boundary that applications may implement using a shared store."""

    def acquire(
        self, key: Hashable, *, policy: RateLimitPolicy, estimated_tokens: int,
        provider: str, model_id: str,
    ) -> RateLimitPermitPort: ...

    def snapshot(self, key: Hashable) -> QuotaSnapshot: ...


@dataclass(slots=True)
class _Reservation:
    admitted_at: float
    tokens: int
    provider: str
    model_id: str


@dataclass(slots=True)
class _QuotaState:
    policy: RateLimitPolicy
    minute_requests: deque[float] = field(default_factory=deque)
    day_requests: deque[float] = field(default_factory=deque)
    # A heap preserves admission-time expiry when calls finish out of order.
    settled_tokens: list[tuple[float, int, int]] = field(default_factory=list)
    active: dict[int, _Reservation] = field(default_factory=dict)
    token_total: int = 0
    cooldown_until: float = 0.0
    warned: set[str] = field(default_factory=set)
    blocked_warned: set[str] = field(default_factory=set)


class RateLimitPermit:
    """Finish an admitted attempt exactly once, even from competing threads.

    Unknown or malformed usage retains its reservation. A valid actual token
    total may be zero or exceed the estimate. Calling finish after a failed
    request still retains its request charges for the applicable windows.
    """

    __slots__ = ("_limiter", "_key", "_reservation_id")

    def __init__(self, limiter: "RateLimiter", key: Hashable, reservation_id: int) -> None:
        self._limiter = limiter
        self._key = key
        self._reservation_id = reservation_id

    def finish(
        self, *, actual_tokens: int | None = None, cooldown_seconds: float | None = None,
    ) -> None:
        self._limiter._finish(
            self._key, self._reservation_id,
            actual_tokens=actual_tokens, cooldown_seconds=cooldown_seconds,
        )


class RateLimiter:
    """Atomically reserve rolling quotas without sleeping or retrying HTTP calls.

    State is proportional to admitted attempts in the rolling day plus active
    calls and one policy binding per admitted key. Denials create no history.
    In-flight token charges remain reserved until finish, even after a minute;
    settled charges expire sixty seconds after admission. Request timestamps
    expire normally while concurrency continues to protect long running calls.
    Observer and logger callbacks run only after releasing the admission lock.
    """

    def __init__(
        self, *, clock: Callable[[], float] = monotonic, observer: QuotaObserver | None = None,
    ) -> None:
        self._clock = clock
        self._observer = observer
        self._lock = Lock()
        self._emission = local()
        self._states: dict[Hashable, _QuotaState] = {}
        self._next_reservation_id = 0

    def acquire(
        self, key: Hashable, *, policy: RateLimitPolicy, estimated_tokens: int,
        provider: str, model_id: str,
    ) -> RateLimitPermit:
        if not isinstance(policy, RateLimitPolicy):
            raise AIConfigurationError("rate_limits")
        if type(estimated_tokens) is not int or estimated_tokens < 0:
            raise AIConfigurationError("estimated_tokens")
        events: list[QuotaEvent] = []
        warn_blocked = True
        error: AIProviderError | None = None
        permit: RateLimitPermit | None = None
        with self._lock:
            state = self._states.get(key)
            if state is not None and state.policy != policy:
                raise AIConfigurationError("rate_limits")
            if estimated_tokens > policy.tokens_per_minute:
                error = AIProviderError("request_budget_exceeded")
                if state is not None:
                    warn_blocked = self._record_block_warning(state, "tokens_per_minute")
                events.append(QuotaEvent(provider, model_id, "blocked", "tokens_per_minute",
                                         estimated_tokens, policy.tokens_per_minute))
            else:
                now = self._clock()
                if state is not None:
                    self._prune(state, now)
                    block = self._blocked(state, now, estimated_tokens)
                else:
                    block = None
                if block is not None:
                    dimension, used, limit, retry_after = block
                    warn_blocked = self._record_block_warning(state, dimension)
                    error = AIProviderError("local_rate_limit", retry_after_seconds=retry_after)
                    events.append(QuotaEvent(provider, model_id, "blocked", dimension,
                                             used, limit, retry_after))
                else:
                    if state is None:
                        state = _QuotaState(policy)
                        self._states[key] = state
                    state.blocked_warned.clear()
                    reservation_id = self._next_reservation_id
                    self._next_reservation_id += 1
                    state.minute_requests.append(now)
                    state.day_requests.append(now)
                    state.active[reservation_id] = _Reservation(now, estimated_tokens, provider, model_id)
                    state.token_total += estimated_tokens
                    permit = RateLimitPermit(self, key, reservation_id)
                    events.extend(self._warnings(state, provider, model_id))
        self._emit(events, warn_blocked=warn_blocked)
        if error is not None:
            raise error
        assert permit is not None
        return permit

    def snapshot(self, key: Hashable) -> QuotaSnapshot:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return QuotaSnapshot(0, 0, 0, 0, 0.0)
            now = self._clock()
            self._prune(state, now)
            return QuotaSnapshot(
                len(state.minute_requests), len(state.day_requests), state.token_total,
                len(state.active), max(0.0, state.cooldown_until - now),
            )

    def _finish(
        self, key: Hashable, reservation_id: int, *, actual_tokens: int | None,
        cooldown_seconds: float | None,
    ) -> None:
        events: list[QuotaEvent] = []
        with self._lock:
            state = self._states.get(key)
            if state is None or reservation_id not in state.active:
                return
            now = self._clock()
            self._prune(state, now)
            reservation = state.active.pop(reservation_id)
            state.token_total -= reservation.tokens
            charge = (actual_tokens if type(actual_tokens) is int and actual_tokens >= 0
                      else reservation.tokens)
            if reservation.admitted_at + _MINUTE > now:
                heapq.heappush(state.settled_tokens, (reservation.admitted_at, reservation_id, charge))
                state.token_total += charge
            duration = self._cooldown_duration(cooldown_seconds)
            if duration is not None:
                deadline = now + duration
                if math.isfinite(deadline) and deadline > max(now, state.cooldown_until):
                    state.cooldown_until = deadline
                    events.append(QuotaEvent(
                        reservation.provider, reservation.model_id, "cooldown", "cooldown",
                        0, 0, deadline - now,
                    ))
            events.extend(self._warnings(state, reservation.provider, reservation.model_id))
        self._emit(events)

    @staticmethod
    def _cooldown_duration(value: float | None) -> float | None:
        if type(value) not in (int, float) or value < 0:
            return None
        try:
            return float(value) if math.isfinite(value) else None
        except OverflowError:
            return None

    @classmethod
    def _prune(cls, state: _QuotaState, now: float) -> None:
        while state.minute_requests and state.minute_requests[0] + _MINUTE <= now:
            state.minute_requests.popleft()
        while state.day_requests and state.day_requests[0] + _DAY <= now:
            state.day_requests.popleft()
        while state.settled_tokens and state.settled_tokens[0][0] + _MINUTE <= now:
            _, _, charge = heapq.heappop(state.settled_tokens)
            state.token_total -= charge
        for dimension, used, limit in cls._dimensions(state):
            if not cls._near_threshold(used, limit, state.policy):
                state.warned.discard(dimension)

    @staticmethod
    def _dimensions(state: _QuotaState) -> tuple[tuple[str, int, int], ...]:
        policy = state.policy
        return (
            ("requests_per_minute", len(state.minute_requests), policy.requests_per_minute),
            ("requests_per_day", len(state.day_requests), policy.requests_per_day),
            ("tokens_per_minute", state.token_total, policy.tokens_per_minute),
            ("max_concurrent", len(state.active), policy.max_concurrent),
        )

    @staticmethod
    def _near_threshold(used: int, limit: int, policy: RateLimitPolicy) -> bool:
        # Preserve the configured decimal boundary: 0.8 means exactly 80%.
        numerator, denominator = Fraction(str(policy.warning_fraction)).as_integer_ratio()
        return used * denominator >= limit * numerator

    @classmethod
    def _warnings(cls, state: _QuotaState, provider: str, model_id: str) -> list[QuotaEvent]:
        events = []
        for dimension, used, limit in cls._dimensions(state):
            if cls._near_threshold(used, limit, state.policy):
                if dimension not in state.warned:
                    state.warned.add(dimension)
                    events.append(QuotaEvent(provider, model_id, "near_limit", dimension, used, limit))
            else:
                state.warned.discard(dimension)
        return events

    @staticmethod
    def _blocked(
        state: _QuotaState, now: float, tokens: int,
    ) -> tuple[str, int, int, float] | None:
        policy = state.policy
        if state.cooldown_until > now:
            return "cooldown", 0, 0, state.cooldown_until - now
        if len(state.active) >= policy.max_concurrent:
            return "max_concurrent", len(state.active), policy.max_concurrent, 1.0
        if len(state.minute_requests) >= policy.requests_per_minute:
            return ("requests_per_minute", len(state.minute_requests), policy.requests_per_minute,
                    state.minute_requests[0] + _MINUTE - now)
        if len(state.day_requests) >= policy.requests_per_day:
            return ("requests_per_day", len(state.day_requests), policy.requests_per_day,
                    state.day_requests[0] + _DAY - now)
        needed = state.token_total + tokens - policy.tokens_per_minute
        if needed > 0:
            released = 0
            for admitted_at, _, charge in sorted(state.settled_tokens):
                released += charge
                if released >= needed:
                    return ("tokens_per_minute", state.token_total, policy.tokens_per_minute,
                            admitted_at + _MINUTE - now)
            # Completion time of active calls is unknown; callers may try later.
            return "tokens_per_minute", state.token_total, policy.tokens_per_minute, 1.0
        return None

    @staticmethod
    def _record_block_warning(state: _QuotaState, dimension: str) -> bool:
        # This set contains only the five fixed quota dimensions. Keep every
        # denial observable while bounding console noise until admission resumes.
        should_warn = dimension not in state.blocked_warned
        state.blocked_warned.add(dimension)
        return should_warn

    def _emit(self, events: list[QuotaEvent], *, warn_blocked: bool = True) -> None:
        # Observers/handlers may re-enter admission. Avoid recursively notifying
        # the same thread while allowing independent threads to emit normally.
        if getattr(self._emission, "active", False):
            return
        self._emission.active = True
        try:
            for event in events:
                if event.kind != "blocked" or warn_blocked:
                    try:
                        _LOGGER.warning(
                            "AI quota %s: provider=%s model=%s %s=%s/%s retry_after_seconds=%s",
                            event.kind, event.provider, event.model_id, event.dimension,
                            event.used, event.limit, event.retry_after_seconds,
                            extra={
                                "quota_event": "ai.quota",
                                "quota_provider": event.provider,
                                "quota_model_id": event.model_id,
                                "quota_kind": event.kind,
                                "quota_dimension": event.dimension,
                                "quota_used": event.used,
                                "quota_limit": event.limit,
                                "quota_retry_after_seconds": event.retry_after_seconds,
                            },
                        )
                    except Exception:
                        # Monitoring must never change admission or release capacity.
                        pass
                if self._observer is not None:
                    try:
                        self._observer(event)
                    except Exception:
                        pass
        finally:
            self._emission.active = False
