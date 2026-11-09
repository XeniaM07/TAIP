"""Independent verification guards domain ambiguity and report amplification."""

import json
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from taip.common.timetable_contracts import (
    ConstraintSeverity, ConstraintType, LessonAssignment, MaxClassPeriodsPerDay, MinimizeTeacherGaps,
    TeacherPreferredSlots, TeacherUnavailable, TimeSlot, TimetableConstraint,
    VerificationStatus, Weekday,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from tests.mathematical_verification.test_timetable_verifier import load_fixture


class VerificationDomainIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot, self.solution = load_fixture()
        self.verifier = TimetableVerifier()

    def verify_data(self, **updates):
        return self.verifier.verify(
            replace(self.snapshot, data=replace(self.snapshot.data, **updates)),
            self.solution,
        )

    def test_different_slot_ids_at_same_coordinate_cannot_be_verified(self):
        data = self.snapshot.data
        slots = tuple(
            replace(slot, day=data.time_slots[0].day, period=1)
            if slot.slot_id == "tue-1" else slot
            for slot in data.time_slots
        )
        result = self.verify_data(time_slots=slots)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("snapshot.duplicate_slot_coordinates", {item.code for item in result.violations})

    def test_malformed_slot_shapes_fail_without_later_handler_errors(self):
        data = self.snapshot.data
        for changes in (
            {"day": "saturday"}, {"period": 0}, {"period": True},
            {"start_time": "9:00"}, {"end_time": "25:00"},
            {"start_time": "10:00", "end_time": "09:00"},
        ):
            with self.subTest(changes=changes):
                result = self.verify_data(time_slots=(replace(data.time_slots[0], **changes),) + data.time_slots[1:])
                self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_explicit_overlapping_intervals_cannot_be_verified(self):
        data = self.snapshot.data
        result = self.verify_data(time_slots=(
            replace(data.time_slots[0], start_time="08:00", end_time="09:00"),
            replace(data.time_slots[1], start_time="08:30", end_time="09:30"),
        ) + data.time_slots[2:])
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_unknown_subject_on_unused_teacher_and_negative_unused_capacity_fail(self):
        data = self.snapshot.data
        result = self.verify_data(teachers=data.teachers + (
            replace(data.teachers[0], teacher_id="unused", subject_ids=("unknown",)),
        ))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        result = self.verify_data(rooms=data.rooms + (
            replace(data.rooms[0], room_id="unused", capacity=-1),
        ))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        result = self.verify_data(classes=data.classes + (
            replace(data.classes[0], class_id="unused", student_count=-1),
        ))
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_empty_teacher_subjects_and_partial_times_are_not_invented(self):
        data = self.snapshot.data
        result = self.verify_data(
            teachers=tuple(replace(item, subject_ids=()) for item in data.teachers),
            time_slots=(replace(data.time_slots[0], start_time="08:00"),) + data.time_slots[1:],
        )
        self.assertEqual(result.status, VerificationStatus.VERIFIED)

    def test_huge_direct_snapshot_is_rejected_before_occurrence_range_allocation(self):
        data = self.snapshot.data
        with patch("taip.mathematical_verification.timetable_chain.range", side_effect=AssertionError("unbounded occurrence range"), create=True):
            result = self.verify_data(requirements=(replace(data.requirements[0], sessions_per_week=1_000_000_000),))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("snapshot.lesson_resource_limit", {item.code for item in result.violations})

    def test_oversized_total_or_solution_skips_all_later_handlers(self):
        data = self.snapshot.data
        snapshot = replace(self.snapshot, data=replace(data,
            requirements=tuple(replace(data.requirements[0], requirement_id=f"req-{index}", sessions_per_week=1000) for index in range(11)),
        ))
        with patch("taip.mathematical_verification.timetable_chain.ResourceConflictHandler._verify", side_effect=AssertionError("unsafe later stage")):
            result = self.verifier.verify(snapshot, self.solution)
            self.assertEqual(result.status, VerificationStatus.FAILED)
            result = self.verifier.verify(self.snapshot, replace(self.solution,
                assignments=(self.solution.assignments[0],) * 10_001,
            ))
            self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_thousand_repeated_occurrences_have_bounded_report_and_no_false_attachments(self):
        requirement = replace(self.snapshot.data.requirements[0], sessions_per_week=1000)
        snapshot = replace(self.snapshot, data=replace(self.snapshot.data, requirements=(requirement,), constraints=()))
        solution = replace(self.solution, assignments=(LessonAssignment(requirement.requirement_id, 0, "mon-1", "r101"),) * 1000)
        result = self.verifier.verify(snapshot, solution)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertLessEqual(len(result.violations), 500)
        self.assertLessEqual(sum(len(item.lessons) for item in result.violations), 2000)
        missing = [item for item in result.violations if item.code == "requirement.missing_occurrence"]
        self.assertTrue(missing)
        self.assertTrue(all(item.requirement_ids == (requirement.requirement_id,) and item.lessons == () for item in missing))
        self.assertLess(len(json.dumps(asdict(result)).encode()), 550_000)

    @staticmethod
    def soft_constraints(count):
        return tuple(TimetableConstraint(
            f"soft-{index}", ConstraintType.TEACHER_PREFERRED_SLOTS,
            ConstraintSeverity.SOFT, TeacherPreferredSlots("t-math", ()),
        ) for index in range(count))

    def test_soft_finding_flood_cannot_hide_late_hard_constraint(self):
        hard = TimetableConstraint("late-hard", ConstraintType.TEACHER_UNAVAILABLE,
            ConstraintSeverity.HARD, TeacherUnavailable("t-math", ("mon-1",)))
        result = self.verify_data(constraints=self.soft_constraints(600) + (hard,))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertTrue(any(item.constraint_id == "late-hard" for item in result.violations))
        self.assertLessEqual(len(result.violations), 500)

    def test_incomplete_soft_report_is_inconclusive_and_stops_computation(self):
        result = self.verify_data(constraints=self.soft_constraints(1000))
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertLessEqual(len(result.violations), 500)
        self.assertLess(result.soft_penalty, 3000)
        self.assertTrue(any("truncat" in note.lower() for note in result.notes))

    def test_many_constraint_attachments_fit_serialized_detail_budget(self):
        constraints = tuple(TimetableConstraint(
            f"soft-{'x' * 1000}-{index}", ConstraintType.TEACHER_PREFERRED_SLOTS,
            ConstraintSeverity.SOFT, TeacherPreferredSlots("t-math", ()),
        ) for index in range(600))
        result = self.verify_data(constraints=constraints)
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertLess(len(json.dumps(asdict(result)).encode()), 512 * 1024)

    def test_attached_lesson_budget_stops_repeated_wide_constraints(self):
        requirement = replace(self.snapshot.data.requirements[0], sessions_per_week=1000)
        slots = tuple(TimeSlot(f"slot-{index}", Weekday.MONDAY, index + 1) for index in range(1000))
        constraints = tuple(TimetableConstraint(
            f"soft-{index}", ConstraintType.MAX_CLASS_PERIODS_PER_DAY,
            ConstraintSeverity.SOFT, MaxClassPeriodsPerDay(requirement.class_id, 1),
        ) for index in range(25))
        snapshot = replace(self.snapshot, data=replace(self.snapshot.data,
            requirements=(requirement,), time_slots=slots, constraints=constraints,
        ))
        solution = replace(self.solution, assignments=tuple(
            LessonAssignment(requirement.requirement_id, index, slot.slot_id, "r101")
            for index, slot in enumerate(slots)
        ))
        result = self.verifier.verify(snapshot, solution)
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertLessEqual(sum(len(item.lessons) for item in result.violations), 2000)
        self.assertLess(len(result.violations), 25)
        self.assertLess(len(json.dumps(asdict(result)).encode()), 512 * 1024)

    def gap_case(self, period, weight):
        requirement = self.snapshot.data.requirements[0]
        slots = (TimeSlot("first", Weekday.MONDAY, 1), TimeSlot("last", Weekday.MONDAY, period))
        constraints = (TimetableConstraint(
            "teacher-gaps", ConstraintType.MINIMIZE_TEACHER_GAPS,
            ConstraintSeverity.SOFT, MinimizeTeacherGaps(requirement.teacher_id), weight,
        ),)
        snapshot = replace(self.snapshot, data=replace(self.snapshot.data,
            requirements=(requirement,), time_slots=slots, constraints=constraints,
        ))
        solution = replace(self.solution, assignments=tuple(
            LessonAssignment(requirement.requirement_id, index, slot.slot_id, "r101")
            for index, slot in enumerate(slots)
        ))
        return snapshot, solution

    def test_huge_period_and_weight_fail_before_penalty_arithmetic(self):
        snapshot, solution = self.gap_case(10 ** 3000, 10 ** 3000)
        result = self.verifier.verify(snapshot, solution)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.soft_penalty, 0)
        self.assertIn("snapshot.period_resource_limit", {item.code for item in result.violations})
        self.assertIn("snapshot.weight_resource_limit", {item.code for item in result.violations})

    def test_huge_weight_or_daily_maximum_fails_even_without_large_periods(self):
        snapshot, solution = self.gap_case(4, 10 ** 3000)
        result = self.verifier.verify(snapshot, solution)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.soft_penalty, 0)
        maximum = TimetableConstraint(
            "daily-max", ConstraintType.MAX_CLASS_PERIODS_PER_DAY,
            ConstraintSeverity.HARD, MaxClassPeriodsPerDay("5A", 10 ** 3000),
        )
        result = self.verify_data(constraints=(maximum,))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("snapshot.daily_periods_resource_limit", {item.code for item in result.violations})

    def test_supported_period_and_weight_endpoints_retain_exact_penalties(self):
        snapshot, solution = self.gap_case(10_000, 1_000_000)
        result = self.verifier.verify(snapshot, solution)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertEqual(result.soft_penalty, 9_998_000_000)

    def test_oversized_class_size_and_capacity_cannot_escape_as_rendering_errors(self):
        data = self.snapshot.data
        result = self.verify_data(classes=(
            replace(data.classes[0], student_count=10 ** 5000),
        ) + data.classes[1:])
        self.assertEqual(result.status, VerificationStatus.FAILED)
        result = self.verify_data(rooms=(
            replace(data.rooms[0], capacity=10 ** 5000),
        ) + data.rooms[1:])
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_oversized_occurrence_is_reported_without_rendering_its_integer(self):
        assignments = (replace(self.solution.assignments[0], occurrence=10 ** 5000),) + self.solution.assignments[1:]
        result = self.verifier.verify(self.snapshot, replace(self.solution, assignments=assignments))
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertLess(len(json.dumps(asdict(result)).encode()), 512 * 1024)
        unknown_sizes = replace(self.snapshot, data=replace(self.snapshot.data,
            classes=tuple(replace(item, student_count=None) for item in self.snapshot.data.classes),
        ))
        result = self.verifier.verify(unknown_sizes, replace(self.solution, assignments=assignments))
        self.assertEqual(result.status, VerificationStatus.FAILED)
