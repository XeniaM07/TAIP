"""AOP monitoring for timetable verification operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import wraps
from time import perf_counter
from typing import Callable, Protocol, TypeVar

from taip.common.timetable_contracts import (
    TimetableProblemSnapshot,
    TimetableSolution,
    TimetableVerificationResult,
)


_Result = TypeVar("_Result", bound=TimetableVerificationResult)
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VerificationAuditEvent:
    operation: str
    duration_ms: float
    status: str
    violation_count: int


class VerificationAuditLog:
    """In-memory monitor for verification duration and outcomes."""

    def __init__(self) -> None:
        self.events: list[VerificationAuditEvent] = []

    def record(self, event: VerificationAuditEvent) -> None:
        self.events.append(event)


class _MonitoredVerifier(Protocol):
    audit_log: VerificationAuditLog


def monitored_verify(
    method: Callable[
        [_MonitoredVerifier, TimetableProblemSnapshot, TimetableSolution],
        _Result,
    ],
) -> Callable[
    [_MonitoredVerifier, TimetableProblemSnapshot, TimetableSolution],
    _Result,
]:
    """Measure and log verification without changing its input or result."""

    @wraps(method)
    def wrapper(
        self: _MonitoredVerifier,
        snapshot: TimetableProblemSnapshot,
        solution: TimetableSolution,
    ) -> _Result:
        started = perf_counter()
        try:
            result = method(self, snapshot, solution)
        except Exception:
            duration_ms = round((perf_counter() - started) * 1000, 3)
            self.audit_log.record(
                VerificationAuditEvent(
                    operation="verify",
                    duration_ms=duration_ms,
                    status="error",
                    violation_count=0,
                )
            )
            _logger.exception("Timetable verification raised an exception.")
            raise

        duration_ms = round((perf_counter() - started) * 1000, 3)
        self.audit_log.record(
            VerificationAuditEvent(
                operation="verify",
                duration_ms=duration_ms,
                status=result.status.value,
                violation_count=len(result.violations),
            )
        )
        _logger.info(
            "Timetable verification completed: status=%s violations=%d duration_ms=%.3f",
            result.status.value,
            len(result.violations),
            duration_ms,
        )
        return result

    return wrapper
