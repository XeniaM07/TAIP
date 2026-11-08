"""Strategy interface and default policy for timetable verification."""

from abc import ABC, abstractmethod

from taip.common.timetable_contracts import (
    TimetableProblemSnapshot,
    TimetableSolution,
    TimetableVerificationResult,
)
from taip.mathematical_verification.timetable_chain import (
    AssignmentIntegrityHandler,
    ConstraintVerificationHandler,
    ResourceConflictHandler,
    VerificationContext,
)


class TimetableVerificationStrategy(ABC):
    """Strategy contract for verifying timetable snapshots and solutions."""

    @abstractmethod
    def verify(
        self,
        snapshot: TimetableProblemSnapshot,
        solution: TimetableSolution,
    ) -> TimetableVerificationResult:
        ...


class DefaultTimetableVerificationStrategy(TimetableVerificationStrategy):
    """Apply structural, resource, and configured rule checks."""

    def verify(
        self,
        snapshot: TimetableProblemSnapshot,
        solution: TimetableSolution,
    ) -> TimetableVerificationResult:
        context = VerificationContext(snapshot=snapshot, solution=solution)
        integrity = AssignmentIntegrityHandler()
        conflicts = integrity.set_next(ResourceConflictHandler())
        conflicts.set_next(ConstraintVerificationHandler())
        integrity.handle(context)
        return TimetableVerificationResult(
            solution_id=solution.solution_id,
            snapshot_id=snapshot.snapshot_id,
            status=context.status,
            violations=tuple(context.violations),
            soft_penalty=context.soft_penalty,
            notes=tuple(dict.fromkeys(context.notes)),
        )
