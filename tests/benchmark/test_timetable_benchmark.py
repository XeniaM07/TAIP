"""Reproducible tests for independent timetable benchmark comparisons."""

import csv
import json
import unittest

from taip.benchmark.models import VerificationAuditMetrics
from taip.benchmark.observer import (
    BenchmarkProgressObserver,
    TimetableComparisonCollector,
)
from taip.benchmark.repository import InMemoryBenchmarkRepository
from taip.benchmark.timetable_service import TimetableBenchmarkService
from taip.common.timetable_contracts import (
    ConstraintViolation,
    ConstraintSeverity,
    ConstraintType,
    ExtractionProposal,
    LessonAssignment,
    ModelRunResult,
    RoomFeatureRequired,
    TeacherPreferredSlots,
    TeacherUnavailable,
    TimetableConstraint,
    TimetableData,
    TimetableSolution,
    TimetableVerificationResult,
    VerificationStatus,
)
from taip.common.timetable_protocols import BenchmarkPort, EditOperation
from taip.mathematical_verification.timetable_audit import VerificationAuditEvent


def expected_constraints() -> tuple[TimetableConstraint, ...]:
    return (
        TimetableConstraint(
            "model-generated-1",
            ConstraintType.ROOM_FEATURE_REQUIRED,
            ConstraintSeverity.HARD,
            RoomFeatureRequired("req-2", "computers"),
            source_text="lab",
        ),
        TimetableConstraint(
            "model-generated-2",
            ConstraintType.TEACHER_UNAVAILABLE,
            ConstraintSeverity.HARD,
            TeacherUnavailable("t-math", ("mon-3",)),
        ),
    )


def proposal(
    model_id: str,
    constraints: tuple[TimetableConstraint, ...],
) -> ExtractionProposal:
    return ExtractionProposal(
        proposal_id=f"proposal-{model_id}",
        problem_id="school-demo",
        model_id=model_id,
        data=TimetableData(constraints=constraints),
    )


class TimetableBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryBenchmarkRepository()
        self.service = TimetableBenchmarkService(
            session_id="session-a",
            repository=self.repository,
        )
        self.collector = TimetableComparisonCollector()
        self.progress = BenchmarkProgressObserver()
        self.service.result_publisher.attach(self.collector)
        self.service.result_publisher.attach(self.progress)

    def record_mock_runs(self) -> tuple[ExtractionProposal, ...]:
        reference = expected_constraints()
        proposals = (
            proposal("gemini", reference),
            proposal(
                "llama",
                (
                    reference[0],
                    TimetableConstraint(
                        "extra",
                        ConstraintType.TEACHER_PREFERRED_SLOTS,
                        ConstraintSeverity.SOFT,
                        TeacherPreferredSlots("t-math", ("tue-1",)),
                    ),
                ),
            ),
            proposal("qwen", ()),
        )
        edits = (
            (
                EditOperation(
                    "edit-g1", "room", "lab", "update", "before", "after"
                ),
            ),
            (
                EditOperation(
                    "edit-l1", "teacher", "t-math", "update", "before", "after"
                ),
                EditOperation(
                    "edit-l2", "constraint", "c-new", "add", None, "after"
                ),
            ),
            (
                EditOperation(
                    "edit-q1", "class", "7B", "add", None, "after"
                ),
                EditOperation(
                    "edit-q2", "room", "lab", "remove", "before", None
                ),
                EditOperation(
                    "edit-q3", "constraint", "c-old", "remove", "before", None
                ),
            ),
        )
        statuses = (
            (VerificationStatus.VERIFIED, ()),
            (
                VerificationStatus.FAILED,
                (
                    ("teacher_overlap", ConstraintSeverity.HARD),
                    ("soft_gap", ConstraintSeverity.SOFT),
                ),
            ),
            (VerificationStatus.INCONCLUSIVE, ()),
        )

        for index, (model, proposal_value) in enumerate(
            zip(("gemini", "llama", "qwen"), proposals)
        ):
            self.service.record_extraction(
                "cmp-1", proposal_value, duration_ms=2 + index
            )
            for operation in edits[index]:
                self.service.record_edit("cmp-1", model, operation)
            solution = TimetableSolution(
                solution_id=f"solution-{model}",
                snapshot_id=f"snapshot-{model}",
                model_id=model,
                assignments=(
                    LessonAssignment("req-1", 0, "mon-1", "r101"),
                ),
            )
            self.service.record_solution(
                "cmp-1", solution, duration_ms=50 + 10 * index
            )
            status, violation_specs = statuses[index]
            result = TimetableVerificationResult(
                solution_id=solution.solution_id,
                snapshot_id=solution.snapshot_id,
                status=status,
                violations=tuple(
                    ConstraintViolation(
                        code=code,
                        message=f"{code} found",
                        requirement_ids=("req-1",),
                        slot_ids=("mon-1",),
                        severity=severity,
                        lessons=solution.assignments,
                    )
                    for code, severity in violation_specs
                ),
                soft_penalty=3 if model == "llama" else 0,
            )
            event = VerificationAuditMetrics(
                duration_ms=5 + index,
                status=status.value,
                violation_count=len(result.violations),
            )
            self.service.record_verification(
                "cmp-1", model, result, audit_event=event
            )
        return proposals

    def test_three_mock_runs_generate_separate_reproducible_metrics(self) -> None:
        proposals = self.record_mock_runs()
        report = self.service.compare_runs("cmp-1", "school-demo")

        self.assertEqual(
            tuple(row.model_id for row in report.rows),
            ("gemini", "llama", "qwen"),
        )
        self.assertEqual(report.snapshots_comparable, False)
        self.assertIn("different confirmed snapshots", report.notes[0])

        gemini, llama, qwen = report.rows
        self.assertIs(
            self.repository.get_run("session-a", "cmp-1", "gemini").proposal,
            proposals[0],
        )
        self.assertEqual(gemini.edit_count, 1)
        self.assertEqual(
            [(item.entity_type, item.action, item.count) for item in gemini.edit_types],
            [("room", "update", 1)],
        )
        self.assertEqual(llama.edit_count, 2)
        self.assertEqual(qwen.edit_count, 3)
        self.assertEqual(gemini.verification_status, "verified")
        self.assertEqual(llama.verification_status, "failed")
        self.assertEqual(qwen.verification_status, "inconclusive")
        self.assertEqual(llama.hard_violation_count, 1)
        self.assertEqual(llama.soft_violation_count, 1)
        self.assertEqual(llama.soft_penalty, 3)
        self.assertEqual(gemini.total_duration_ms, 57)
        self.assertEqual(llama.total_duration_ms, 69)
        self.assertEqual(qwen.total_duration_ms, 81)

    def test_constraint_reference_metrics_are_separate_not_aggregate_score(self) -> None:
        self.record_mock_runs()
        report = self.service.compare_runs("cmp-1", "school-demo")
        perfect, partial, empty = (
            row.extraction_constraints for row in report.rows
        )

        self.assertEqual(
            (perfect.true_positives, perfect.false_positives, perfect.false_negatives),
            (2, 0, 0),
        )
        self.assertEqual(perfect.precision, 1.0)
        self.assertEqual(perfect.recall, 1.0)
        self.assertEqual(perfect.f1, 1.0)
        self.assertEqual(
            (partial.true_positives, partial.false_positives, partial.false_negatives),
            (1, 1, 1),
        )
        self.assertAlmostEqual(partial.f1, 0.5)
        self.assertEqual(empty.f1, 0.0)
        self.assertFalse(hasattr(report, "score"))

    def test_observers_receive_template_method_lifecycle_and_report(self) -> None:
        self.record_mock_runs()
        report = self.service.compare_runs("cmp-1", "school-demo")

        self.assertEqual(
            [event.state for event in self.progress.events],
            ["running", "completed"],
        )
        self.assertIsNone(self.progress.events[-1].result)
        self.assertIs(self.progress.events[-1].comparison_report, report)
        self.assertEqual(self.collector.reports, [report])

    def test_json_and_csv_exports_are_deterministic(self) -> None:
        self.record_mock_runs()
        report = self.service.compare_runs("cmp-1", "school-demo")
        first_json = self.service.export_json(report)
        first_csv = self.service.export_csv(report)

        self.assertEqual(first_json, self.service.export_json(report))
        self.assertEqual(first_csv, self.service.export_csv(report))
        parsed = json.loads(first_json)
        self.assertEqual(parsed["session_id"], "session-a")
        self.assertEqual(len(parsed["rows"]), 3)
        csv_rows = list(csv.DictReader(first_csv.splitlines()))
        self.assertEqual([row["model_id"] for row in csv_rows], [
            "gemini", "llama", "qwen"
        ])
        self.assertEqual(csv_rows[1]["hard_violation_count"], "1")
        self.assertEqual(csv_rows[1]["soft_violation_count"], "1")

    def test_sessions_are_isolated_in_shared_repository(self) -> None:
        other_session = TimetableBenchmarkService(
            session_id="session-b",
            repository=self.repository,
        )
        other_session.record_extraction(
            "cmp-1", proposal("gemini", expected_constraints())
        )

        self.service.record_extraction(
            "cmp-1", proposal("gemini", expected_constraints())
        )
        own = self.service.compare_runs("cmp-1", "school-demo")
        other = other_session.compare_runs("cmp-1", "school-demo")

        self.assertEqual(len(own.rows), 1)
        self.assertEqual(len(other.rows), 1)
        self.assertNotEqual(own.session_id, other.session_id)
        self.assertIsNone(own.rows[0].solving_duration_ms)

    def test_equal_snapshots_are_marked_comparable(self) -> None:
        for model in ("gemini", "llama"):
            self.service.record_extraction(
                "cmp-same", proposal(model, expected_constraints())
            )
            solution = TimetableSolution(
                f"solution-{model}", "shared-snapshot", model, ()
            )
            self.service.record_solution("cmp-same", solution, duration_ms=1)
            self.service.record_verification(
                "cmp-same",
                model,
                TimetableVerificationResult(
                    solution.solution_id,
                    solution.snapshot_id,
                    VerificationStatus.VERIFIED,
                ),
                duration_ms=1,
            )

        report = self.service.compare_runs("cmp-same", "school-demo")
        self.assertTrue(report.snapshots_comparable)
        self.assertEqual(report.notes, ())

    def test_reference_set_contains_multiple_typed_interpretations(self) -> None:
        from taip.benchmark.reference_data import TIMETABLE_REFERENCE_CASES

        self.assertGreaterEqual(len(TIMETABLE_REFERENCE_CASES), 2)
        self.assertEqual(
            {case.problem_id for case in TIMETABLE_REFERENCE_CASES},
            {"school-demo", "daily-class-limit"},
        )
        self.assertTrue(
            all(
                case.data.classes
                and case.data.teachers
                and case.data.requirements
                and case.data.constraints
                for case in TIMETABLE_REFERENCE_CASES
            )
        )

    def test_published_port_records_and_compares_legacy_run_summaries(self) -> None:
        self.service.record_run(
            "legacy-cmp",
            ModelRunResult(
                model_id="gemini",
                proposal_id="proposal-gemini",
                verification_status=VerificationStatus.FAILED,
                edit_count=2,
            ),
        )

        result = self.service.compare("legacy-cmp", "school-demo")

        self.assertEqual(result.comparison_id, "legacy-cmp")
        self.assertEqual(result.problem_id, "school-demo")
        self.assertEqual(len(result.model_runs), 1)
        self.assertEqual(result.model_runs[0].model_id, "gemini")
        self.assertEqual(result.model_runs[0].verification_status, VerificationStatus.FAILED)
        self.assertEqual(result.model_runs[0].edit_count, 2)

    def test_verifier_aop_event_can_supply_verification_duration(self) -> None:
        self.service.record_extraction(
            "aop-cmp", proposal("gemini", expected_constraints())
        )
        solution = TimetableSolution("aop-sol", "aop-snapshot", "gemini", ())
        self.service.record_solution("aop-cmp", solution, duration_ms=10)
        verification = TimetableVerificationResult(
            solution.solution_id,
            solution.snapshot_id,
            VerificationStatus.VERIFIED,
        )
        self.service.record_verification(
            "aop-cmp",
            "gemini",
            verification,
            audit_event=VerificationAuditEvent(
                operation="verify",
                duration_ms=2.5,
                status="verified",
                violation_count=0,
            ),
        )

        report = self.service.compare_runs("aop-cmp", "school-demo")
        self.assertEqual(report.rows[0].verification_duration_ms, 2.5)

    def test_operations_reject_duplicates_and_mismatched_results(self) -> None:
        proposal_value = proposal("gemini", expected_constraints())
        self.service.record_extraction("cmp-1", proposal_value)
        with self.assertRaises(ValueError):
            self.service.record_extraction("cmp-1", proposal_value)
        with self.assertRaises(KeyError):
            self.service.record_solution(
                "cmp-1",
                TimetableSolution("sol", "snap", "llama", ()),
                duration_ms=1,
            )

        solution = TimetableSolution("sol", "snap", "gemini", ())
        self.service.record_solution("cmp-1", solution, duration_ms=1)
        with self.assertRaises(ValueError):
            self.service.record_verification(
                "cmp-1",
                "gemini",
                TimetableVerificationResult(
                    "other", "snap", VerificationStatus.VERIFIED
                ),
                duration_ms=1,
            )

    def test_exports_reject_reports_from_other_sessions(self) -> None:
        report = TimetableBenchmarkService("another-session").compare_runs(
            "cmp-empty", "school-demo"
        )
        with self.assertRaises(ValueError):
            self.service.export_json(report)
        with self.assertRaises(ValueError):
            self.service.export_csv(report)

    def test_service_implements_published_benchmark_port(self) -> None:
        self.assertIsInstance(self.service, BenchmarkPort)


if __name__ == "__main__":
    unittest.main()
