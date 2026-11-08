"""In-memory AOP auditing for timetable operations."""

from dataclasses import dataclass
from functools import wraps
from time import perf_counter


@dataclass(frozen=True, slots=True)
class AuditEvent:
    operation: str
    draft_id: str
    duration_ms: float
    success: bool


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def audit_operation(operation_name: str):
    """Audit a service operation without logging sensitive data."""

    def decorator(method):
        @wraps(method)
        def wrapper(self, draft_id: str, *args, **kwargs):
            start = perf_counter()
            success = False

            try:
                result = method(self, draft_id, *args, **kwargs)
                success = True
                return result
            finally:
                duration_ms = (perf_counter() - start) * 1000

                self.audit_log.record(
                    AuditEvent(
                       operation=(
                           args[0].action
                           if operation_name == "edit" and args
                           else operation_name
                        ),
                        draft_id=draft_id,
                        duration_ms=round(duration_ms, 3),
                        success=success,
                    )
                )

        return wrapper

    return decorator