"""Draft confirmation must reject structurally ambiguous timetable data."""

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from taip.common.timetable_contracts import (
    ConstraintSeverity, ConstraintType, ExtractionProposal, MaxClassPeriodsPerDay,
    MinimizeTeacherGaps, TimetableConstraint,
)
from taip.problem_management.repository import InMemoryProblemRepository
from taip.problem_management.service import TimetableProblemService
from tests.mathematical_verification.test_timetable_verifier import load_fixture


class DraftDomainIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot, _ = load_fixture()
        self.service = TimetableProblemService(InMemoryProblemRepository())

    def validate_data(self, data):
        draft = self.service.create_draft(
            ExtractionProposal("proposal", "problem", "gemini", data)
        )
        return draft, self.service.validate(draft.draft_id)

    def assert_invalid(self, data, code):
        draft, result = self.validate_data(data)
        self.assertFalse(result.valid)
        self.assertIn(code, {issue.code for issue in result.issues})
        with self.assertRaises(ValueError):
            self.service.confirm(draft.draft_id, "Build a timetable")

    def test_duplicate_day_period_cannot_be_confirmed(self):
        data = self.snapshot.data
        alias = replace(data.time_slots[0], slot_id="same-time-new-id")
        self.assert_invalid(
            replace(data, time_slots=data.time_slots + (alias,)),
            "DUPLICATE_SLOT_COORDINATES",
        )

    def test_invalid_clock_day_and_period_are_rejected(self):
        data = self.snapshot.data
        for updates, code in (
            ({"day": "saturday"}, "INVALID_SLOT_DAY"),
            ({"period": 0}, "INVALID_SLOT_PERIOD"),
            ({"period": True}, "INVALID_SLOT_PERIOD"),
            ({"period": 1.5}, "INVALID_SLOT_PERIOD"),
            ({"start_time": "8:00"}, "INVALID_SLOT_TIME"),
            ({"start_time": "24:00"}, "INVALID_SLOT_TIME"),
            ({"end_time": "10:60"}, "INVALID_SLOT_TIME"),
            ({"start_time": "09:00", "end_time": "09:00"}, "INVALID_SLOT_INTERVAL"),
            ({"start_time": "10:00", "end_time": "09:00"}, "INVALID_SLOT_INTERVAL"),
        ):
            with self.subTest(updates=updates):
                slot = replace(data.time_slots[0], **updates)
                self.assert_invalid(
                    replace(data, time_slots=(slot,) + data.time_slots[1:]), code
                )

    def test_explicit_intervals_must_not_overlap(self):
        data = self.snapshot.data
        slots = (
            replace(data.time_slots[0], start_time="08:00", end_time="09:00"),
            replace(data.time_slots[1], start_time="08:30", end_time="09:30"),
        ) + data.time_slots[2:]
        self.assert_invalid(replace(data, time_slots=slots), "OVERLAPPING_SLOT_INTERVALS")

    def test_partial_clocks_and_touching_intervals_remain_valid(self):
        data = self.snapshot.data
        slots = (
            replace(data.time_slots[0], start_time="08:00", end_time="09:00"),
            replace(data.time_slots[1], start_time="09:00", end_time="10:00"),
            replace(data.time_slots[2], start_time="10:00"),
        ) + data.time_slots[3:]
        _, result = self.validate_data(replace(data, time_slots=slots))
        self.assertTrue(result.valid, result.issues)

    def test_unknown_teacher_subject_reference_cannot_be_confirmed(self):
        data = self.snapshot.data
        teacher = replace(data.teachers[0], subject_ids=("not-a-subject",))
        self.assert_invalid(
            replace(data, teachers=(teacher,) + data.teachers[1:]),
            "UNKNOWN_TEACHER_SUBJECT",
        )

    def test_known_class_sizes_and_capacities_must_be_nonnegative_integers(self):
        data = self.snapshot.data
        for value in (-1, True, 1.5):
            with self.subTest(value=value):
                self.assert_invalid(
                    replace(data, classes=(replace(data.classes[0], student_count=value),) + data.classes[1:]),
                    "INVALID_CLASS_SIZE",
                )
                self.assert_invalid(
                    replace(data, rooms=(replace(data.rooms[0], capacity=value),) + data.rooms[1:]),
                    "INVALID_ROOM_CAPACITY",
                )

    def test_optional_values_and_empty_teacher_subjects_are_allowed(self):
        data = self.snapshot.data
        _, result = self.validate_data(replace(
            data,
            classes=tuple(replace(item, student_count=None) for item in data.classes),
            rooms=tuple(replace(item, capacity=None) for item in data.rooms),
            teachers=tuple(replace(item, subject_ids=()) for item in data.teachers),
        ))
        self.assertTrue(result.valid, result.issues)

    def test_weekly_counts_over_supported_budget_cannot_be_confirmed(self):
        data = self.snapshot.data
        self.assert_invalid(replace(
            data,
            requirements=(replace(data.requirements[0], sessions_per_week=1_000_000_000),),
        ), "LESSON_RESOURCE_LIMIT")
        self.assert_invalid(replace(
            data,
            requirements=tuple(replace(data.requirements[0], requirement_id=f"req-{index}", sessions_per_week=1000) for index in range(11)),
        ), "LESSON_RESOURCE_LIMIT")

    def test_numeric_magnitudes_are_bounded_before_confirmation(self):
        data = self.snapshot.data
        enormous = 10 ** 3000
        self.assert_invalid(replace(
            data, time_slots=(replace(data.time_slots[0], period=enormous),) + data.time_slots[1:],
        ), "PERIOD_RESOURCE_LIMIT")
        self.assert_invalid(replace(data, constraints=(TimetableConstraint(
            "heavy", ConstraintType.MINIMIZE_TEACHER_GAPS, ConstraintSeverity.SOFT,
            MinimizeTeacherGaps("t-math"), weight=enormous,
        ),)), "WEIGHT_RESOURCE_LIMIT")
        self.assert_invalid(replace(data, constraints=(TimetableConstraint(
            "maximum", ConstraintType.MAX_CLASS_PERIODS_PER_DAY, ConstraintSeverity.HARD,
            MaxClassPeriodsPerDay("5A", enormous),
        ),)), "DAILY_PERIODS_RESOURCE_LIMIT")
        self.assert_invalid(replace(data, classes=(
            replace(data.classes[0], student_count=10 ** 5000),
        ) + data.classes[1:]), "CLASS_SIZE_RESOURCE_LIMIT")
        self.assert_invalid(replace(data, rooms=(
            replace(data.rooms[0], capacity=10 ** 5000),
        ) + data.rooms[1:]), "ROOM_CAPACITY_RESOURCE_LIMIT")


class NumericMagnitudeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_json_with_3000_digit_numbers_cannot_confirm_or_call_solver(self):
        from taip.application.api import create_app
        from taip.application.workflow import (
            MOCK_DEMO_PROMPT, MOCK_MODEL_IDS, InMemorySessionRepository,
            MockProviderFactory, MockTimetableProvider, WorkflowService,
        )
        from taip.mathematical_verification.timetable_verifier import TimetableVerifier

        service = WorkflowService(
            InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test",
        ) as client:
            created = await client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})
            session_id = created.json()["session_id"]
            model_id = MOCK_MODEL_IDS[0]
            extracted = await client.post(
                f"/api/sessions/{session_id}/extract", json={"model_ids": [model_id]},
            )
            data = extracted.json()["models"][model_id]["draft"]["data"]
            enormous = 10 ** 2999
            data["time_slots"][1]["period"] = enormous
            data["constraints"] = [{
                "constraint_id": "huge-gap-penalty",
                "type": "minimize_teacher_gaps", "severity": "soft",
                "parameters": {"teacher_id": "teacher-1"}, "weight": enormous,
            }]
            body = json.dumps({"data": data}).encode()
            self.assertLess(len(body), 64 * 1024)
            self.assertEqual(len(str(enormous)), 3000)
            edited = await client.patch(
                f"/api/sessions/{session_id}/drafts/{model_id}",
                content=body, headers={"Content-Type": "application/json"},
            )
            self.assertEqual(edited.status_code, 200, edited.text)
            with patch.object(MockTimetableProvider, "solve", side_effect=AssertionError("solver must not run")):
                confirmed = await client.post(
                    f"/api/sessions/{session_id}/models/{model_id}/confirm",
                )
                self.assertEqual(confirmed.status_code, 400, confirmed.text)
                self.assertIn("RESOURCE_LIMIT", confirmed.json()["detail"])
                solved = await client.post(
                    f"/api/sessions/{session_id}/models/{model_id}/solve",
                )
                self.assertEqual(solved.status_code, 400, solved.text)
