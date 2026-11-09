"""Per-call AI observations, isolated from provider behavior and private text.

Records are delivered to a caller-owned observer; this module keeps no history.
Only safe identifiers, timing, outcomes, and error categories enter logging.
"""

from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from functools import wraps
import logging
from time import perf_counter_ns
from typing import Concatenate, Literal, ParamSpec, Protocol, TypeVar
from uuid import uuid4

from .errors import AIConfigurationError, AIProviderError, AIResponseError
from .telemetry import GenerationParameters, ProviderUsage


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    """An invocation snapshot whose private content is excluded from repr."""

    call_id: str
    operation: str
    provider: str
    model_id: str
    requested_model_id: str
    returned_model_id: str | None
    upstream_provider: str | None
    duration_ms: float
    outcome: str
    error_code: str | None
    http_status_code: int | None
    retry_after_seconds: float | None
    result: object | None = field(default=None, repr=False)
    raw_response: str | None = field(default=None, repr=False)
    generation_parameters: GenerationParameters | None = None
    usage: ProviderUsage | None = None
    settlement_pending: bool = False
    settlement_warning: str | None = None


InvocationObserver = Callable[[InvocationRecord], None]
_scoped_observers: ContextVar[tuple[InvocationObserver, ...]] = ContextVar("ai_scoped_observers", default=())


@contextmanager
def observe_invocations(observer: InvocationObserver):
    """Observe only calls in this context, without retaining global history."""
    if not callable(observer):
        raise AIConfigurationError("observer")
    token = _scoped_observers.set(_scoped_observers.get() + (observer,))
    try:
        yield
    finally:
        _scoped_observers.reset(token)


class _InvocationSource(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def requested_model_id(self) -> str: ...

    @property
    def invocation_observer(self) -> InvocationObserver | None: ...


@dataclass(slots=True)
class _ResponseCapture:
    raw_response: str | None = field(default=None, repr=False)
    returned_model_id: str | None = None
    upstream_provider: str | None = None
    generation_parameters: GenerationParameters | None = None
    usage: ProviderUsage | None = None
    settlement_pending: bool = False


_active_capture: ContextVar[_ResponseCapture | None] = ContextVar(
    "ai_invocation_response", default=None
)
_logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_Source = TypeVar("_Source", bound=_InvocationSource)


def capture_provider_response(
    *,
    raw_response: str | None = None,
    returned_model_id: str | None = None,
    upstream_provider: str | None = None,
    generation_parameters: GenerationParameters | None = None,
    usage: ProviderUsage | None = None,
) -> None:
    """Attach available response data to the current invocation, if any."""
    capture = _active_capture.get()
    if capture is None:
        return
    if raw_response is not None:
        capture.raw_response = raw_response
    if returned_model_id is not None:
        capture.returned_model_id = returned_model_id
    if upstream_provider is not None:
        capture.upstream_provider = upstream_provider
    if generation_parameters is not None:
        capture.generation_parameters = generation_parameters
    if usage is not None:
        capture.usage = usage


def capture_quota_settlement_pending() -> None:
    """Flag accounting uncertainty without changing the provider's outcome."""
    capture = _active_capture.get()
    if capture is not None:
        capture.settlement_pending = True


def _error_metadata(
    error: BaseException | None,
) -> tuple[str | None, int | None, float | None]:
    if error is None:
        return None, None, None
    if isinstance(error, AIProviderError):
        return error.code, error.status_code, error.retry_after_seconds
    if isinstance(error, (AIResponseError, AIConfigurationError)):
        return error.code, None, None
    return "unexpected_error", None, None


def _log_safely(level: int, message: str, *, extra: dict[str, object] | None = None) -> None:
    try:
        _logger.log(level, message, extra=extra)
    except Exception:
        # A handler is external to the provider operation and may itself fail.
        pass


def _publish(record: InvocationRecord, observer: InvocationObserver | None) -> None:
    # A nested call has already restored its parent's context. Suspend capture
    # during delivery so callbacks and log handlers cannot mutate that parent.
    token = _active_capture.set(None)
    scoped = _scoped_observers.get()
    scoped_token = _scoped_observers.set(())
    try:
        _log_safely(
            logging.WARNING if record.settlement_pending else logging.INFO,
            "ai.invocation",
            extra={
                "ai_call_id": record.call_id,
                "ai_operation": record.operation,
                "ai_provider": record.provider,
                "ai_model_id": record.model_id,
                "ai_requested_model_id": record.requested_model_id,
                "ai_returned_model_id": record.returned_model_id,
                "ai_upstream_provider": record.upstream_provider,
                "ai_duration_ms": record.duration_ms,
                "ai_outcome": record.outcome,
                "ai_error_code": record.error_code,
                "ai_http_status_code": record.http_status_code,
                "ai_retry_after_seconds": record.retry_after_seconds,
                "ai_generation_parameters": asdict(record.generation_parameters) if record.generation_parameters else None,
                "ai_usage": asdict(record.usage) if record.usage else None,
                "ai_settlement_pending": record.settlement_pending,
                "ai_settlement_warning": record.settlement_warning,
            },
        )
        if observer is not None:
            try:
                observer(record)
            except Exception:
                _log_safely(logging.WARNING, "AI invocation observer failed.")
        for callback in scoped:
            try:
                callback(record)
            except Exception:
                _log_safely(logging.WARNING, "AI invocation observer failed.")
    finally:
        _scoped_observers.reset(scoped_token)
        _active_capture.reset(token)


def monitor_ai_call(
    operation: Literal["extract", "solve"],
) -> Callable[
    [Callable[Concatenate[_Source, _P], _R]],
    Callable[Concatenate[_Source, _P], _R],
]:
    """Observe a synchronous provider method without replacing its result."""

    def decorate(
        method: Callable[Concatenate[_Source, _P], _R],
    ) -> Callable[Concatenate[_Source, _P], _R]:
        @wraps(method)
        def monitored(self: _Source, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
            capture = _ResponseCapture()
            token = _active_capture.set(capture)
            started_ns = perf_counter_ns()
            result: _R | None = None
            failure: BaseException | None = None
            try:
                result = method(self, *args, **kwargs)
                return result
            except BaseException as error:
                failure = error
                raise
            finally:
                duration_ms = (perf_counter_ns() - started_ns) / 1_000_000
                _active_capture.reset(token)
                error_code, status_code, retry_after = _error_metadata(failure)
                raw_response = capture.raw_response
                if raw_response is None and isinstance(failure, AIResponseError):
                    raw_response = failure.raw_response
                record = InvocationRecord(
                    call_id=uuid4().hex,
                    operation=operation,
                    provider=self.provider_name,
                    model_id=self.model_id,
                    requested_model_id=self.requested_model_id,
                    returned_model_id=capture.returned_model_id,
                    upstream_provider=capture.upstream_provider,
                    duration_ms=duration_ms,
                    outcome="success" if failure is None else "error",
                    error_code=error_code,
                    http_status_code=status_code,
                    retry_after_seconds=retry_after,
                    result=result,
                    raw_response=raw_response,
                    generation_parameters=capture.generation_parameters,
                    usage=capture.usage,
                    settlement_pending=capture.settlement_pending,
                    settlement_warning="quota_settlement_pending" if capture.settlement_pending else None,
                )
                _publish(record, self.invocation_observer)

        return monitored

    return decorate
