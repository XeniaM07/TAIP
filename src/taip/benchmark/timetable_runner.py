"""Template Method for reproducible timetable comparison reports."""

from uuid import uuid4

from taip.benchmark.models import (
    ConstraintExtractionMetrics,
    EditTypeCount,
    TimetableComparisonReport,
    TimetableModelMetrics,
    TimetableReferenceCase,
    TimetableRunRecord,
    constraint_signature,
)
from taip.benchmark.observer import BenchmarkEvent, BenchmarkSubject
from taip.benchmark.repository import InMemoryBenchmarkRepository
from taip.common.timetable_contracts import ConstraintSeverity


class TimetableComparisonRunner:
    """Run the fixed load, evaluate, and report workflow for a comparison."""

    def __init__(
        self,
        session_id: str,
        comparison_id: str,
        problem_id: str,
        repository: InMemoryBenchmarkRepository,
        result_publisher: BenchmarkSubject,
        reference_case: TimetableReferenceCase | None,
    ) -> None:
        self.session_id = session_id
        self.comparison_id = comparison_id
        self.problem_id = problem_id
        self.repository = repository
        self.result_publisher = result_publisher
        self.reference_case = reference_case

    def run_comparison(self) -> TimetableComparisonReport:
        """Execute all report phases and publish lifecycle events."""
        execution_id = str(uuid4())
        self.result_publisher.notify_observers(
            BenchmarkEvent(
                benchmark_id=self.comparison_id,
                execution_id=execution_id,
                state="running",
                result=None,
            )
        )
        try:
            runs = self.load_runs()
            rows = self.evaluate_runs(runs)
            report = self.generate_report(rows)
        except Exception:
            self.result_publisher.notify_observers(
                BenchmarkEvent(
                    benchmark_id=self.comparison_id,
                    execution_id=execution_id,
                    state="failed",
                    result=None,
                )
            )
            raise

        self.result_publisher.notify_observers(
            BenchmarkEvent(
                benchmark_id=self.comparison_id,
                execution_id=execution_id,
                state="completed",
                result=None,
                comparison_report=report,
            )
        )
        return report

    def load_runs(self) -> tuple[TimetableRunRecord, ...]:
        """Load only this session's runs for the requested problem."""
        return self.repository.list_runs(
            self.session_id, self.comparison_id, self.problem_id
        )

    def evaluate_runs(
        self, runs: tuple[TimetableRunRecord, ...]
    ) -> tuple[TimetableModelMetrics, ...]:
        """Build independent metrics rows without an aggregate score."""
        rows: list[TimetableModelMetrics] = []
        for run in runs:
            edit_counts: dict[tuple[str, str], int] = {}
            for edit in run.edits:
                key = (edit.entity_type, edit.action)
                edit_counts[key] = edit_counts.get(key, 0) + 1
            edit_types = tuple(
                EditTypeCount(entity_type, action, count)
                for (entity_type, action), count in sorted(edit_counts.items())
            )

            verification = run.verification
            hard_violations = (
                sum(
                    violation.severity == ConstraintSeverity.HARD
                    for violation in verification.violations
                )
                if verification is not None
                else 0
            )
            soft_violations = (
                sum(
                    violation.severity == ConstraintSeverity.SOFT
                    for violation in verification.violations
                )
                if verification is not None
                else 0
            )
            durations = (
                run.extraction_duration_ms,
                run.solving_duration_ms,
                run.verification_duration_ms,
            )
            total_duration = (
                sum(duration for duration in durations if duration is not None)
                if all(duration is not None for duration in durations)
                else None
            )
            rows.append(
                TimetableModelMetrics(
                    session_id=run.session_id,
                    comparison_id=run.comparison_id,
                    problem_id=run.problem_id,
                    model_id=run.model_id,
                    proposal_id=run.proposal.proposal_id,
                    snapshot_id=(
                        verification.snapshot_id
                        if verification is not None
                        else run.solution.snapshot_id
                        if run.solution is not None
                        else None
                    ),
                    solution_id=(
                        verification.solution_id
                        if verification is not None
                        else run.solution.solution_id
                        if run.solution is not None
                        else None
                    ),
                    edit_count=len(run.edits),
                    edit_types=edit_types,
                    verification_status=(
                        verification.status.value if verification is not None else None
                    ),
                    hard_violation_count=hard_violations,
                    soft_violation_count=soft_violations,
                    soft_penalty=(
                        verification.soft_penalty
                        if verification is not None
                        else None
                    ),
                    extraction_duration_ms=run.extraction_duration_ms,
                    solving_duration_ms=run.solving_duration_ms,
                    verification_duration_ms=run.verification_duration_ms,
                    total_duration_ms=total_duration,
                    extraction_constraints=(
                        self._constraint_metrics(run, self.reference_case)
                        if self.reference_case is not None
                        else None
                    ),
                )
            )
        return tuple(rows)

    def generate_report(
        self, rows: tuple[TimetableModelMetrics, ...]
    ) -> TimetableComparisonReport:
        """Create the deterministic table and make comparability explicit."""
        snapshot_ids = {row.snapshot_id for row in rows}
        snapshots_comparable = (
            len(snapshot_ids) == 1 and None not in snapshot_ids
            if rows
            else None
        )
        notes: list[str] = []
        if not rows:
            notes.append("No model runs were recorded for this comparison.")
        if self.reference_case is None:
            notes.append(
                "No reference interpretation is registered for this problem; "
                "constraint extraction metrics are unavailable."
            )
        if snapshots_comparable is False:
            notes.append(
                "Runs use different confirmed snapshots. Verification and soft "
                "penalties are reported per run and are not directly comparable."
            )
        return TimetableComparisonReport(
            session_id=self.session_id,
            comparison_id=self.comparison_id,
            problem_id=self.problem_id,
            rows=rows,
            snapshots_comparable=snapshots_comparable,
            notes=tuple(notes),
        )

    @staticmethod
    def _constraint_metrics(
        run: TimetableRunRecord,
        reference_case: TimetableReferenceCase,
    ) -> ConstraintExtractionMetrics | None:
        if run.problem_id != reference_case.problem_id:
            return None
        expected = [constraint_signature(item) for item in reference_case.constraints]
        actual = [
            constraint_signature(item) for item in run.proposal.data.constraints
        ]
        expected_counts: dict[tuple[object, ...], int] = {}
        actual_counts: dict[tuple[object, ...], int] = {}
        for signature in expected:
            expected_counts[signature] = expected_counts.get(signature, 0) + 1
        for signature in actual:
            actual_counts[signature] = actual_counts.get(signature, 0) + 1

        true_positives = sum(
            min(expected_count, actual_counts.get(signature, 0))
            for signature, expected_count in expected_counts.items()
        )
        false_positives = len(actual) - true_positives
        false_negatives = len(expected) - true_positives
        precision = (
            true_positives / len(actual) if actual else float(not expected)
        )
        recall = (
            true_positives / len(expected) if expected else float(not actual)
        )
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        return ConstraintExtractionMetrics(
            true_positives=true_positives,
            false_positives=false_positives,
            false_negatives=false_negatives,
            precision=precision,
            recall=recall,
            f1=f1,
        )
