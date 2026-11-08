"""Functional tests for the independent timetable verifier."""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from taip.common.timetable_contracts import (
    BalanceClassDailyLoad,
    ClassUnavailable,
    ConstraintParameters,
    ConstraintSeverity,
    ConstraintType,
    LessonAssignment,
    MaxClassPeriodsPerDay,
    MinimizeClassGaps,
    MinimizeTeacherGaps,
    Room,
    RoomFeatureRequired,
    RoomUnavailable,
    SchoolClass,
    Subject,
    Teacher,
    TeacherPreferredSlots,
    TeacherUnavailable,
    TeachingRequirement,
    TimetableConstraint,
    TimetableData,
    TimetableProblemSnapshot,
    TimetableSolution,
    TimetableVerificationResult,
    TimeSlot,
    VerificationStatus,
    Weekday,
)
from taip.common.timetable_protocols import VerificationPort
from taip.mathematical_verification.timetable_audit import VerificationAuditLog
from taip.mathematical_verification.timetable_chain import (
    ConstraintVerificationHandler as TimetableConstraintVerificationHandler,
    TimetableVerificationHandler,
    VerificationContext,
)
from taip.mathematical_verification.timetable_strategies import (
    DefaultTimetableVerificationStrategy,
    TimetableVerificationStrategy,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "timetable"


def load_fixture() -> tuple[TimetableProblemSnapshot, TimetableSolution]:
    raw_snapshot = json.loads(
        (FIXTURES / "confirmed_snapshot.json").read_text(encoding="utf-8")
    )
    raw_data = raw_snapshot["data"]
    parameter_classes = {
        ConstraintType.TEACHER_UNAVAILABLE: TeacherUnavailable,
        ConstraintType.ROOM_UNAVAILABLE: RoomUnavailable,
        ConstraintType.CLASS_UNAVAILABLE: ClassUnavailable,
        ConstraintType.ROOM_FEATURE_REQUIRED: RoomFeatureRequired,
        ConstraintType.MAX_CLASS_PERIODS_PER_DAY: MaxClassPeriodsPerDay,
        ConstraintType.TEACHER_PREFERRED_SLOTS: TeacherPreferredSlots,
        ConstraintType.MINIMIZE_CLASS_GAPS: MinimizeClassGaps,
        ConstraintType.BALANCE_CLASS_DAILY_LOAD: BalanceClassDailyLoad,
    }

    def constraints() -> tuple[TimetableConstraint, ...]:
        parsed: list[TimetableConstraint] = []
        for item in raw_data["constraints"]:
            constraint_type = ConstraintType(item["type"])
            parameters = dict(item["parameters"])
            if "slot_ids" in parameters:
                parameters["slot_ids"] = tuple(parameters["slot_ids"])
            parsed.append(
                TimetableConstraint(
                    constraint_id=item["constraint_id"],
                    type=constraint_type,
                    severity=ConstraintSeverity(item["severity"]),
                    parameters=parameter_classes[constraint_type](**parameters),
                    weight=item["weight"],
                    source_text=item["source_text"],
                )
            )
        return tuple(parsed)

    data = TimetableData(
        classes=tuple(
            SchoolClass(**item)
            for item in raw_data["classes"]
        ),
        teachers=tuple(
            Teacher(**{**item, "subject_ids": tuple(item["subject_ids"])})
            for item in raw_data["teachers"]
        ),
        subjects=tuple(
            Subject(**item)
            for item in raw_data["subjects"]
        ),
        rooms=tuple(
            Room(**{**item, "features": tuple(item["features"])})
            for item in raw_data["rooms"]
        ),
        time_slots=tuple(
            TimeSlot(**{**item, "day": Weekday(item["day"])})
            for item in raw_data["time_slots"]
        ),
        requirements=tuple(
            TeachingRequirement(**item)
            for item in raw_data["requirements"]
        ),
        constraints=constraints(),
    )
    snapshot = TimetableProblemSnapshot(
        **{**raw_snapshot, "data": data}
    )
    raw_solution = json.loads(
        (FIXTURES / "valid_solution.json").read_text(encoding="utf-8")
    )
    solution = TimetableSolution(
        **{
            **raw_solution,
            "assignments": tuple(
                LessonAssignment(**item) for item in raw_solution["assignments"]
            ),
        }
    )
    return snapshot, solution


class TimetableVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot, self.solution = load_fixture()
        self.verifier = TimetableVerifier()

    def with_constraint(
        self,
        constraint_id: str,
        constraint_type: ConstraintType,
        parameters: ConstraintParameters,
        severity: ConstraintSeverity = ConstraintSeverity.HARD,
        weight: int = 1,
        base: TimetableProblemSnapshot | None = None,
    ) -> TimetableProblemSnapshot:
        constraint = TimetableConstraint(
            constraint_id=constraint_id,
            type=constraint_type,
            severity=severity,
            parameters=parameters,
            weight=weight,
        )
        source = base if base is not None else self.snapshot
        return replace(
            source,
            data=replace(
                source.data,
                constraints=source.data.constraints + (constraint,),
            ),
        )

    def test_valid_fixture_passes_and_implements_public_port(self) -> None:
        result = self.verifier.verify(self.snapshot, self.solution)

        self.assertIsInstance(self.verifier, VerificationPort)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(result.violations, ())
        self.assertEqual(result.soft_penalty, 0)

    def test_invalid_fixture_reports_conflicts_and_hard_constraints(self) -> None:
        raw_solution = json.loads(
            (FIXTURES / "invalid_solution.json").read_text(encoding="utf-8")
        )
        solution = replace(
            self.solution,
            solution_id=raw_solution["solution_id"],
            assignments=tuple(
                LessonAssignment(**item) for item in raw_solution["assignments"]
            ),
        )

        result = self.verifier.verify(self.snapshot, solution)
        codes = {violation.code for violation in result.violations}

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertTrue(
            {
                "schedule.teacher_overlap",
                "schedule.class_overlap",
                "schedule.room_overlap",
                "constraint.teacher_unavailable",
                "constraint.room_feature",
            }.issubset(codes)
        )
        for violation in result.violations:
            if violation.code.startswith("schedule."):
                self.assertGreaterEqual(len(violation.lessons), 2)

    def test_missing_and_duplicate_occurrences_are_reported(self) -> None:
        assignments = list(self.solution.assignments)
        assignments[1] = replace(assignments[1], occurrence=0)
        solution = replace(self.solution, assignments=tuple(assignments))

        result = self.verifier.verify(self.snapshot, solution)
        codes = {violation.code for violation in result.violations}

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("assignment.duplicate_occurrence", codes)
        self.assertIn("requirement.missing_occurrence", codes)

    def test_malformed_occurrence_is_reported_without_crashing(self) -> None:
        assignments = list(self.solution.assignments)
        assignments[0] = replace(assignments[0], occurrence=cast(int, []))
        result = self.verifier.verify(
            self.snapshot,
            replace(self.solution, assignments=tuple(assignments)),
        )

        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn(
            "assignment.occurrence_out_of_range",
            {item.code for item in result.violations},
        )

    def test_missing_lesson_reports_exact_required_count(self) -> None:
        solution = replace(
            self.solution,
            assignments=self.solution.assignments[:-1],
        )
        result = self.verifier.verify(self.snapshot, solution)
        missing = [
            item
            for item in result.violations
            if item.code == "requirement.lesson_count"
        ]

        self.assertEqual(len(missing), 1)
        self.assertIn("needs exactly 1 lessons; found 0", missing[0].message)
        self.assertEqual(missing[0].requirement_ids, ("req-4",))

    def test_unknown_slot_and_room_are_reported_with_affected_lesson(self) -> None:
        assignments = list(self.solution.assignments)
        assignments[0] = replace(
            assignments[0], slot_id="unknown-slot", room_id="unknown-room"
        )
        result = self.verifier.verify(
            self.snapshot,
            replace(self.solution, assignments=tuple(assignments)),
        )
        by_code = {item.code: item for item in result.violations}

        self.assertIn("assignment.unknown_slot", by_code)
        self.assertIn("assignment.unknown_room", by_code)
        self.assertEqual(by_code["assignment.unknown_slot"].lessons, (assignments[0],))

    def test_teacher_room_and_class_unavailability_are_checked(self) -> None:
        constraint_specs = (
            (
                "teacher-away",
                ConstraintType.TEACHER_UNAVAILABLE,
                TeacherUnavailable("t-math", ("mon-1",)),
            ),
            (
                "room-away",
                ConstraintType.ROOM_UNAVAILABLE,
                RoomUnavailable("r101", ("mon-1",)),
            ),
            (
                "class-away",
                ConstraintType.CLASS_UNAVAILABLE,
                ClassUnavailable("5A", ("mon-1",)),
            ),
        )
        snapshot = self.snapshot
        for constraint_id, constraint_type, parameters in constraint_specs:
            snapshot = self.with_constraint(
                constraint_id, constraint_type, parameters, base=snapshot
            )

        result = self.verifier.verify(snapshot, self.solution)
        found = {
            violation.code
            for violation in result.violations
            if violation.constraint_id in {"teacher-away", "room-away", "class-away"}
        }
        self.assertEqual(
            found,
            {
                "constraint.teacher_unavailable",
                "constraint.room_unavailable",
                "constraint.class_unavailable",
            },
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_capacity_and_daily_maximum_are_checked(self) -> None:
        rooms = tuple(
            replace(room, capacity=20) if room.room_id == "r101" else room
            for room in self.snapshot.data.rooms
        )
        snapshot = replace(
            self.snapshot,
            data=replace(self.snapshot.data, rooms=rooms),
        )
        snapshot = self.with_constraint(
            "max-day",
            ConstraintType.MAX_CLASS_PERIODS_PER_DAY,
            MaxClassPeriodsPerDay("5A", 1),
            base=snapshot,
        )

        result = self.verifier.verify(snapshot, self.solution)
        codes = {item.code for item in result.violations}
        self.assertIn("room.capacity", codes)
        self.assertIn("constraint.max_class_periods_per_day", codes)

    def test_unknown_capacity_makes_result_inconclusive(self) -> None:
        classes = tuple(
            replace(school_class, student_count=None)
            for school_class in self.snapshot.data.classes
        )
        snapshot = replace(
            self.snapshot,
            data=replace(self.snapshot.data, classes=classes),
        )
        result = self.verifier.verify(snapshot, self.solution)

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertTrue(any("capacity cannot be checked" in note for note in result.notes))

    def test_soft_constraints_are_penalized_without_failing(self) -> None:
        snapshot = self.with_constraint(
            "preferred",
            ConstraintType.TEACHER_PREFERRED_SLOTS,
            TeacherPreferredSlots("t-math", ("mon-3",)),
            severity=ConstraintSeverity.SOFT,
            weight=2,
        )
        snapshot = replace(
            snapshot,
            data=replace(
                snapshot.data,
                constraints=snapshot.data.constraints
                + (
                    TimetableConstraint(
                        "balance",
                        ConstraintType.BALANCE_CLASS_DAILY_LOAD,
                        ConstraintSeverity.SOFT,
                        BalanceClassDailyLoad("5A"),
                        weight=2,
                    ),
                ),
            ),
        )

        result = self.verifier.verify(snapshot, self.solution)
        soft_findings = [
            item
            for item in result.violations
            if item.severity == ConstraintSeverity.SOFT
        ]

        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(result.soft_penalty, 8)
        self.assertTrue(soft_findings)
        self.assertTrue(all(item.constraint_id for item in soft_findings))

    def test_gap_constraint_reports_lesson_pair(self) -> None:
        assignments = list(self.solution.assignments)
        assignments[1] = replace(assignments[1], slot_id="mon-3")
        assignments[2] = replace(assignments[2], slot_id="tue-3")
        solution = replace(self.solution, assignments=tuple(assignments))
        snapshot = self.with_constraint(
            "class-gaps",
            ConstraintType.MINIMIZE_CLASS_GAPS,
            MinimizeClassGaps("5A"),
            severity=ConstraintSeverity.SOFT,
            base=replace(
                self.snapshot,
                data=replace(
                    self.snapshot.data,
                    constraints=tuple(
                        item
                        for item in self.snapshot.data.constraints
                        if item.type != ConstraintType.MINIMIZE_CLASS_GAPS
                    ),
                ),
            ),
        )
        snapshot = replace(
            snapshot,
            data=replace(
                snapshot.data,
                constraints=tuple(
                    item
                    for item in snapshot.data.constraints
                    if item.constraint_id != "con-2"
                ),
            ),
        )

        result = self.verifier.verify(snapshot, solution)
        gaps = [item for item in result.violations if item.code == "soft.class_gaps"]

        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(len(gaps[0].lessons), 2)
        self.assertEqual(result.soft_penalty, 1)

    def test_teacher_gap_constraint_is_evaluated(self) -> None:
        assignments = list(self.solution.assignments)
        assignments[1] = replace(assignments[1], slot_id="mon-3")
        assignments[5] = replace(assignments[5], slot_id="tue-3")
        solution = replace(self.solution, assignments=tuple(assignments))
        snapshot = replace(
            self.snapshot,
            data=replace(
                self.snapshot.data,
                constraints=tuple(
                    item
                    for item in self.snapshot.data.constraints
                    if item.constraint_id != "con-2"
                )
                + (
                    TimetableConstraint(
                        "teacher-gaps",
                        ConstraintType.MINIMIZE_TEACHER_GAPS,
                        ConstraintSeverity.SOFT,
                        MinimizeTeacherGaps("t-math"),
                        weight=3,
                    ),
                ),
            ),
        )

        result = self.verifier.verify(snapshot, solution)
        gaps = [
            item for item in result.violations
            if item.code == "soft.teacher_gaps"
        ]

        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].constraint_id, "teacher-gaps")
        self.assertEqual(len(gaps[0].lessons), 2)
        self.assertEqual(result.soft_penalty, 3)

    def test_solution_and_result_are_not_mutated_by_verification(self) -> None:
        snapshot_before = self.snapshot
        solution_before = self.solution

        result = self.verifier.verify(self.snapshot, self.solution)

        self.assertIsInstance(result, TimetableVerificationResult)
        self.assertEqual(self.snapshot, snapshot_before)
        self.assertEqual(self.solution, solution_before)

    def test_aop_records_status_duration_and_violation_count(self) -> None:
        audit_log = VerificationAuditLog()
        verifier = TimetableVerifier(audit_log=audit_log)
        result = verifier.verify(self.snapshot, self.solution)

        event = audit_log.events[-1]
        self.assertEqual(event.operation, "verify")
        self.assertEqual(event.status, result.status.value)
        self.assertEqual(event.violation_count, len(result.violations))
        self.assertGreaterEqual(event.duration_ms, 0)

    def test_chain_handler_is_abstract_and_continues_to_next_stage(self) -> None:
        self.assertTrue(TimetableVerificationHandler.__abstractmethods__)
        handler = TimetableConstraintVerificationHandler()
        context = VerificationContext(self.snapshot, self.solution)
        handler.handle(context)
        self.assertIsNotNone(context.status)

    def test_default_strategy_is_replaceable(self) -> None:
        self.assertIsInstance(
            DefaultTimetableVerificationStrategy(), TimetableVerificationStrategy
        )


if __name__ == "__main__":
    unittest.main()
