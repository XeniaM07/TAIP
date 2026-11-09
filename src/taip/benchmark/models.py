
"""Data structures for independent benchmark evaluation."""

from dataclasses import dataclass, fields

from taip.common.timetable_contracts import (
    ConstraintSeverity,
    ConstraintType,
    ExtractionProposal,
    TimetableData,
    TimetableConstraint,
    TimetableSolution,
    TimetableVerificationResult,
)
from taip.common.timetable_protocols import EditOperation


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """Represents one test case in a mathematical benchmark."""

    case_id: str
    problem_statement: str
    expected_result: str


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    """Represents a versioned collection of benchmark cases."""

    dataset_id: str
    version: str
    cases: tuple[BenchmarkCase, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Represents the result of evaluating a model on a benchmark."""

    benchmark_id: str
    dataset_version: str
    model_id: str
    total_cases: int
    successful_cases: int
    score: float | None


@dataclass(frozen=True, slots=True)
class TimetableReferenceCase:
    """Ground-truth interpretation used to evaluate extraction quality."""

    problem_id: str
    prompt: str
    data: TimetableData

    @property
    def constraints(self) -> tuple[TimetableConstraint, ...]:
        return self.data.constraints


@dataclass(frozen=True, slots=True)
class EditTypeCount:
    entity_type: str
    action: str
    count: int


@dataclass(frozen=True, slots=True)
class ConstraintExtractionMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True, slots=True)
class TimetableRunRecord:
    """Collected, model-specific data for one comparison in one session."""

    session_id: str
    comparison_id: str
    proposal: ExtractionProposal
    extraction_duration_ms: float | None = None
    edits: tuple[EditOperation, ...] = ()
    solution: TimetableSolution | None = None
    solving_duration_ms: float | None = None
    verification: TimetableVerificationResult | None = None
    verification_duration_ms: float | None = None

    @property
    def problem_id(self) -> str:
        return self.proposal.problem_id

    @property
    def model_id(self) -> str:
        return self.proposal.model_id


@dataclass(frozen=True, slots=True)
class TimetableModelMetrics:
    """Separate measured metrics for one model's timetable flow."""

    session_id: str
    comparison_id: str
    problem_id: str
    model_id: str
    proposal_id: str
    snapshot_id: str | None
    solution_id: str | None
    edit_count: int
    edit_types: tuple[EditTypeCount, ...]
    verification_status: str | None
    hard_violation_count: int
    soft_violation_count: int
    soft_penalty: int | None
    extraction_duration_ms: float | None
    solving_duration_ms: float | None
    verification_duration_ms: float | None
    total_duration_ms: float | None
    extraction_constraints: ConstraintExtractionMetrics | None


@dataclass(frozen=True, slots=True)
class TimetableComparisonReport:
    """Deterministic comparison table without an aggregate score."""

    session_id: str
    comparison_id: str
    problem_id: str
    rows: tuple[TimetableModelMetrics, ...]
    snapshots_comparable: bool | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationAuditMetrics:
    """Structural view of a verification monitor event, without module coupling."""

    duration_ms: float
    status: str
    violation_count: int


def constraint_signature(constraint: TimetableConstraint) -> tuple[object, ...]:
    """Return a stable semantic key; display text and generated IDs are excluded."""
    parameters = constraint.parameters
    parameter_values = tuple(
        sorted(
            (
                item.name,
                (
                    value.value
                    if hasattr(value, "value")
                    else tuple(
                        part.value if hasattr(part, "value") else part
                        for part in value
                    )
                    if isinstance(value, tuple)
                    else value
                ),
            )
            for item in fields(parameters)
            for value in (getattr(parameters, item.name),)
        )
    )
    return (
        constraint.type.value
        if isinstance(constraint.type, ConstraintType)
        else str(constraint.type),
        constraint.severity.value
        if isinstance(constraint.severity, ConstraintSeverity)
        else str(constraint.severity),
        parameter_values,
    )
