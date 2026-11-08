"""Stable boundaries for the five TAIP modules (timetabling MVP v1).

Interfaces only: no HTTP, database, LLM SDK or solver dependencies.
Implementations may be synchronous in v1; orchestration owns the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .timetable_contracts import (
    ComparisonRun,
    ExtractionProposal,
    ModelRunResult,
    OriginalProblem,
    ProblemDraft,
    TimetableData,
    TimetableProblemSnapshot,
    TimetableSolution,
    TimetableVerificationResult,
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class DraftValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class EditOperation:
    """One auditable edit. 'before'/'after' are serialized values or None.

    The problem-management module defines how to apply supported operations;
    use entity_type values from the agreed domain schema.
    """

    operation_id: str
    entity_type: str
    entity_id: str
    action: str  # add | update | remove
    before_json: str | None
    after_json: str | None


@runtime_checkable
class ProblemManagementPort(Protocol):
    def create_draft(self, proposal: ExtractionProposal) -> ProblemDraft: ...

    def get_draft(self, draft_id: str) -> ProblemDraft: ...

    def apply_edit(self, draft_id: str, operation: EditOperation) -> ProblemDraft: ...

    def undo(self, draft_id: str) -> ProblemDraft: ...

    def redo(self, draft_id: str) -> ProblemDraft: ...

    def validate(self, draft_id: str) -> DraftValidationResult: ...

    def confirm(self, draft_id: str, original_prompt: str) -> TimetableProblemSnapshot: ...

    def get_snapshot(self, snapshot_id: str) -> TimetableProblemSnapshot: ...


@runtime_checkable
class AIProviderPort(Protocol):
    @property
    def model_id(self) -> str: ...

    def extract(self, problem: OriginalProblem) -> ExtractionProposal: ...

    def solve(self, snapshot: TimetableProblemSnapshot) -> TimetableSolution: ...


@runtime_checkable
class AIProviderFactoryPort(Protocol):
    def get_provider(self, model_id: str) -> AIProviderPort: ...


@runtime_checkable
class VerificationPort(Protocol):
    def verify(
        self, snapshot: TimetableProblemSnapshot, solution: TimetableSolution
    ) -> TimetableVerificationResult: ...


@runtime_checkable
class BenchmarkPort(Protocol):
    def record_run(self, comparison_id: str, result: ModelRunResult) -> None: ...

    def compare(self, comparison_id: str, problem_id: str) -> ComparisonRun: ...


@runtime_checkable
class OrchestrationPort(Protocol):
    def submit_problem(self, prompt: str) -> OriginalProblem: ...

    def extract_for_model(self, problem_id: str, model_id: str) -> ExtractionProposal: ...

    def confirm_draft(self, draft_id: str) -> TimetableProblemSnapshot: ...

    def solve_and_verify(
        self, snapshot_id: str
    ) -> tuple[TimetableSolution, TimetableVerificationResult]: ...

    def get_comparison(self, comparison_id: str) -> ComparisonRun: ...
