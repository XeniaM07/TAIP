"""Public, provider-independent timetable verification service."""

from taip.common.timetable_contracts import (
    TimetableProblemSnapshot,
    TimetableSolution,
    TimetableVerificationResult,
)
from taip.mathematical_verification.timetable_audit import (
    VerificationAuditLog,
    monitored_verify,
)
from taip.mathematical_verification.timetable_strategies import (
    DefaultTimetableVerificationStrategy,
    TimetableVerificationStrategy,
)


class TimetableVerifier:
    """Verify a timetable independently and monitor each public operation."""

    def __init__(
        self,
        strategy: TimetableVerificationStrategy | None = None,
        audit_log: VerificationAuditLog | None = None,
    ) -> None:
        self.strategy = (
            strategy if strategy is not None else DefaultTimetableVerificationStrategy()
        )
        self.audit_log = audit_log if audit_log is not None else VerificationAuditLog()

    @monitored_verify
    def verify(
        self,
        snapshot: TimetableProblemSnapshot,
        solution: TimetableSolution,
    ) -> TimetableVerificationResult:
        return self.strategy.verify(snapshot, solution)
