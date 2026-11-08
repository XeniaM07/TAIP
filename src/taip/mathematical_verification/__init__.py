"""Public interfaces for mathematical verification strategies and handlers."""

from taip.mathematical_verification.strategies import (
    ConstraintSatisfactionStrategy,
    MathematicalConsistencyStrategy,
    VerificationStrategy,
)
from taip.mathematical_verification.verification_chain import (
    ConstraintVerificationHandler,
    InputValidationHandler,
    MathematicalVerificationHandler,
    VerificationHandler,
)
from taip.mathematical_verification.timetable_chain import (
    AssignmentIntegrityHandler,
    ConstraintVerificationHandler as TimetableConstraintVerificationHandler,
    ResourceConflictHandler,
    TimetableVerificationHandler,
)
from taip.mathematical_verification.timetable_audit import (
    VerificationAuditEvent,
    VerificationAuditLog,
)
from taip.mathematical_verification.timetable_strategies import (
    DefaultTimetableVerificationStrategy,
    TimetableVerificationStrategy,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier

__all__ = [
    "ConstraintSatisfactionStrategy",
    "ConstraintVerificationHandler",
    "DefaultTimetableVerificationStrategy",
    "InputValidationHandler",
    "MathematicalConsistencyStrategy",
    "MathematicalVerificationHandler",
    "AssignmentIntegrityHandler",
    "ResourceConflictHandler",
    "TimetableConstraintVerificationHandler",
    "TimetableVerificationHandler",
    "TimetableVerificationStrategy",
    "TimetableVerifier",
    "VerificationAuditEvent",
    "VerificationAuditLog",
    "VerificationHandler",
    "VerificationStrategy",
]
