"""Session-scoped collection, comparison, and export for timetable runs."""

import csv
import io
import json
import math
from dataclasses import asdict, replace

from taip.benchmark.models import (
    TimetableComparisonReport,
    TimetableReferenceCase,
    TimetableRunRecord,
    VerificationAuditMetrics,
)
from taip.benchmark.observer import BenchmarkResultPublisher
from taip.benchmark.reference_data import TIMETABLE_REFERENCE_CASES
from taip.benchmark.repository import InMemoryBenchmarkRepository
from taip.benchmark.timetable_runner import TimetableComparisonRunner
from taip.common.timetable_contracts import (
    ComparisonRun,
    ExtractionProposal,
    ModelRunResult,
    TimetableSolution,
    TimetableVerificationResult,
)
from taip.common.timetable_protocols import BenchmarkPort, EditOperation


_CSV_FIELDS = (
    "session_id",
    "comparison_id",
    "problem_id",
    "model_id",
    "proposal_id",
    "snapshot_id",
    "solution_id",
    "edit_count",
    "edit_types",
    "verification_status",
    "hard_violation_count",
    "soft_violation_count",
    "soft_penalty",
    "extraction_duration_ms",
    "solving_duration_ms",
    "verification_duration_ms",
    "total_duration_ms",
    "extraction_constraints",
    "snapshots_comparable",
    "notes",
)


class TimetableBenchmarkService:
    """Collect results without persisting them beyond the active session."""

    def __init__(
        self,
        session_id: str,
        repository: InMemoryBenchmarkRepository | None = None,
        result_publisher: BenchmarkResultPublisher | None = None,
        reference_cases: tuple[TimetableReferenceCase, ...] = TIMETABLE_REFERENCE_CASES,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id cannot be empty.")
        self.session_id = session_id
        self.repository = (
            repository if repository is not None else InMemoryBenchmarkRepository()
        )
        self.result_publisher = (
            result_publisher
            if result_publisher is not None
            else BenchmarkResultPublisher()
        )
        self.reference_cases = reference_cases

    def record_extraction(
        self,
        comparison_id: str,
        proposal: ExtractionProposal,
        duration_ms: float | None = None,
    ) -> None:
        """Store the original extraction proposal unchanged."""
        self._validate_identity(comparison_id, proposal.model_id)
        if not proposal.proposal_id.strip() or not proposal.problem_id.strip():
            raise ValueError("proposal_id and problem_id cannot be empty.")
        self._validate_duration(duration_ms, "extraction duration")
        key = (self.session_id, comparison_id, proposal.model_id)
        if self._has_run(*key):
            raise ValueError(
                f"Extraction already recorded for model '{proposal.model_id}' "
                f"in comparison '{comparison_id}'."
            )
        self.repository.save_run(
            TimetableRunRecord(
                session_id=self.session_id,
                comparison_id=comparison_id,
                proposal=proposal,
                extraction_duration_ms=duration_ms,
            )
        )

    def record_edit(
        self,
        comparison_id: str,
        model_id: str,
        operation: EditOperation,
    ) -> None:
        """Append one auditable correction to its model-specific run."""
        if not operation.operation_id.strip():
            raise ValueError("operation_id cannot be empty.")
        if not operation.entity_type.strip() or not operation.action.strip():
            raise ValueError("Edit entity_type and action cannot be empty.")
        record = self._get_run(comparison_id, model_id)
        if any(edit.operation_id == operation.operation_id for edit in record.edits):
            raise ValueError(
                f"Edit '{operation.operation_id}' is already recorded for "
                f"model '{model_id}'."
            )
        self.repository.save_run(replace(record, edits=record.edits + (operation,)))

    def record_solution(
        self,
        comparison_id: str,
        solution: TimetableSolution,
        duration_ms: float,
    ) -> None:
        """Store one model's solution and solve duration."""
        self._validate_duration(duration_ms, "solving duration")
        record = self._get_run(comparison_id, solution.model_id)
        if record.solution is not None:
            raise ValueError(
                f"A solution is already recorded for model '{solution.model_id}'."
            )
        self.repository.save_run(
            replace(
                record,
                solution=solution,
                solving_duration_ms=duration_ms,
            )
        )

    def record_verification(
        self,
        comparison_id: str,
        model_id: str,
        result: TimetableVerificationResult,
        duration_ms: float | None = None,
        audit_event: VerificationAuditMetrics | None = None,
    ) -> None:
        """Store verification output and optionally consume an AOP monitor event."""
        if duration_ms is not None and audit_event is not None:
            raise ValueError(
                "Provide duration_ms or audit_event, not both."
            )
        measured_duration = (
            audit_event.duration_ms if audit_event is not None else duration_ms
        )
        self._validate_duration(measured_duration, "verification duration")
        record = self._get_run(comparison_id, model_id)
        if record.solution is None:
            raise ValueError("Cannot record verification before its solution.")
        if record.verification is not None:
            raise ValueError(
                f"Verification is already recorded for model '{model_id}'."
            )
        if result.solution_id != record.solution.solution_id:
            raise ValueError("Verification result does not match the recorded solution.")
        if result.snapshot_id != record.solution.snapshot_id:
            raise ValueError("Verification result does not match the solution snapshot.")
        if audit_event is not None:
            if audit_event.status != result.status.value:
                raise ValueError("AOP status does not match verification result.")
            if audit_event.violation_count != len(result.violations):
                raise ValueError(
                    "AOP violation count does not match verification result."
                )
        self.repository.save_run(
            replace(
                record,
                verification=result,
                verification_duration_ms=measured_duration,
            )
        )

    def compare_runs(
        self, comparison_id: str, problem_id: str
    ) -> TimetableComparisonReport:
        """Generate deterministic per-model metrics for this active session."""
        self._validate_identity(comparison_id, "comparison")
        if not problem_id.strip():
            raise ValueError("problem_id cannot be empty.")
        runner = TimetableComparisonRunner(
            session_id=self.session_id,
            comparison_id=comparison_id,
            problem_id=problem_id,
            repository=self.repository,
            result_publisher=self.result_publisher,
            reference_case=self._reference_case(problem_id),
        )
        return runner.run_comparison()

    def export_json(self, report: TimetableComparisonReport) -> str:
        """Serialize a comparison report as deterministic UTF-8 JSON text."""
        self._validate_report_session(report)
        return json.dumps(
            asdict(report),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )

    def export_csv(self, report: TimetableComparisonReport) -> str:
        """Serialize the comparison table as deterministic CSV text."""
        self._validate_report_session(report)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=_CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in report.rows:
            values = asdict(row)
            metrics = values["extraction_constraints"]
            writer.writerow(
                {
                    **values,
                    "edit_types": json.dumps(
                        values["edit_types"], ensure_ascii=False, sort_keys=True
                    ),
                    "extraction_constraints": (
                        json.dumps(metrics, sort_keys=True) if metrics is not None else ""
                    ),
                    "snapshots_comparable": (
                        ""
                        if report.snapshots_comparable is None
                        else str(report.snapshots_comparable).lower()
                    ),
                    "notes": " | ".join(report.notes),
                }
            )
        return output.getvalue()

    def record_run(self, comparison_id: str, result: ModelRunResult) -> None:
        """Implement the published BenchmarkPort summary-recording operation."""
        self._validate_identity(comparison_id, result.model_id)
        if any(
            item.model_id == result.model_id
            for item in self.repository.list_legacy_runs(
                self.session_id, comparison_id
            )
        ):
            raise ValueError(
                f"Summary run already recorded for model '{result.model_id}'."
            )
        self.repository.save_legacy_run(self.session_id, comparison_id, result)

    def compare(self, comparison_id: str, problem_id: str) -> ComparisonRun:
        """Implement BenchmarkPort using summaries for this active session."""
        if not problem_id.strip():
            raise ValueError("problem_id cannot be empty.")
        detailed = self.repository.list_runs(
            self.session_id, comparison_id, problem_id
        )
        model_runs = {
            run.model_id: ModelRunResult(
                model_id=run.model_id,
                proposal_id=run.proposal.proposal_id,
                snapshot_id=(
                    run.verification.snapshot_id
                    if run.verification is not None
                    else run.solution.snapshot_id
                    if run.solution is not None
                    else None
                ),
                solution_id=(
                    run.verification.solution_id
                    if run.verification is not None
                    else run.solution.solution_id
                    if run.solution is not None
                    else None
                ),
                verification_status=(
                    run.verification.status if run.verification is not None else None
                ),
                extraction_duration_ms=self._duration_as_int(
                    run.extraction_duration_ms
                ),
                solving_duration_ms=self._duration_as_int(run.solving_duration_ms),
                edit_count=len(run.edits),
            )
            for run in detailed
        }
        model_runs.update(
            {
                result.model_id: result
                for result in self.repository.list_legacy_runs(
                    self.session_id, comparison_id
                )
            }
        )
        return ComparisonRun(
            comparison_id=comparison_id,
            problem_id=problem_id,
            model_runs=tuple(model_runs[key] for key in sorted(model_runs)),
        )

    def _get_run(self, comparison_id: str, model_id: str) -> TimetableRunRecord:
        self._validate_identity(comparison_id, model_id)
        try:
            return self.repository.get_run(
                self.session_id, comparison_id, model_id
            )
        except KeyError as error:
            raise KeyError(
                f"No extraction recorded for model '{model_id}' in comparison "
                f"'{comparison_id}' for session '{self.session_id}'."
            ) from error

    def _has_run(self, session_id: str, comparison_id: str, model_id: str) -> bool:
        try:
            self.repository.get_run(session_id, comparison_id, model_id)
        except KeyError:
            return False
        return True

    def _reference_case(self, problem_id: str) -> TimetableReferenceCase | None:
        return next(
            (
                case
                for case in self.reference_cases
                if case.problem_id == problem_id
            ),
            None,
        )

    def _validate_report_session(self, report: TimetableComparisonReport) -> None:
        if report.session_id != self.session_id:
            raise ValueError("Cannot export a report from another session.")

    @staticmethod
    def _validate_identity(comparison_id: str, model_id: str) -> None:
        if not comparison_id.strip():
            raise ValueError("comparison_id cannot be empty.")
        if not model_id.strip():
            raise ValueError("model_id cannot be empty.")

    @staticmethod
    def _validate_duration(duration_ms: float | None, name: str) -> None:
        if duration_ms is not None and (
            not isinstance(duration_ms, (int, float))
            or isinstance(duration_ms, bool)
            or not math.isfinite(duration_ms)
            or duration_ms < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number.")

    @staticmethod
    def _duration_as_int(duration_ms: float | None) -> int | None:
        return round(duration_ms) if duration_ms is not None else None


assert isinstance(
    TimetableBenchmarkService(session_id="contract-check"), BenchmarkPort
)
