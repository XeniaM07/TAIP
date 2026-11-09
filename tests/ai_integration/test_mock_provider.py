"""Offline provider behavior through the real timetable response boundary."""

import unittest
from copy import deepcopy
from dataclasses import replace

from taip.ai_integration.errors import AIResponseError
from taip.ai_integration.mock_provider import MockAIProvider
from taip.common.timetable_contracts import (
    ExtractionProposal,
    LessonAssignment,
    MissingInformation,
    OriginalProblem,
    Room,
    SchoolClass,
    Subject,
    Teacher,
    TeachingRequirement,
    TimetableData,
    TimetableProblemSnapshot,
    TimetableSolution,
    TimeSlot,
    VerificationStatus,
    Weekday,
)
from taip.common.timetable_protocols import AIProviderPort
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


PROMPT = (
    "Creează un orar pentru clasa A, care are 20 de elevi. "
    "Ana predă Matematică acestei clase, cu două lecții pe săptămână. "
    "Este disponibilă Sala A, cu 25 de locuri și fără dotări speciale. "
    "Intervalele disponibile sunt luni și marți, în fiecare zi perioada "
    "didactică 1, între 08:00 și 08:50. "
    "Solicit și supravegherea automată a pauzei de prânz."
)
EXTRACTION_RESPONSE = """{
  "data": {
    "classes": [{"class_id": "class-a", "name": "A", "student_count": 20}],
    "teachers": [{"teacher_id": "teacher-a", "name": "Ana", "subject_ids": ["math"]}],
    "subjects": [{"subject_id": "math", "name": "Matematică"}],
    "rooms": [{"room_id": "room-a", "name": "Sala A", "capacity": 25, "features": []}],
    "time_slots": [
      {"slot_id": "mon-1", "day": "monday", "period": 1, "start_time": "08:00", "end_time": "08:50"},
      {"slot_id": "tue-1", "day": "tuesday", "period": 1, "start_time": "08:00", "end_time": "08:50"}
    ],
    "requirements": [{"requirement_id": "req-a", "class_id": "class-a", "subject_id": "math", "teacher_id": "teacher-a", "sessions_per_week": 2}],
    "constraints": []
  },
  "missing_information": [{"code": "school.timezone", "message": "Confirmați fusul orar local.", "path": "school.timezone"}],
  "unsupported_requirements": ["Supravegherea automată a pauzei de prânz"]
}"""
SOLUTION_RESPONSE = """{
  "assignments": [
    {"requirement_id": "req-a", "occurrence": 0, "slot_id": "mon-1", "room_id": "room-a"},
    {"requirement_id": "req-a", "occurrence": 1, "slot_id": "tue-1", "room_id": "room-a"}
  ]
}"""
INVALID_SOLUTION_RESPONSE = """{
  "assignments": [
    {"requirement_id": "req-a", "occurrence": 0, "slot_id": "mon-1", "room_id": "room-a"},
    {"requirement_id": "req-a", "occurrence": 1, "slot_id": "mon-1", "room_id": "room-a"}
  ]
}"""


def timetable_data() -> TimetableData:
    """Literal canonical data, independent of the provider's JSON decoder."""
    return TimetableData(
        classes=(SchoolClass("class-a", "A", 20),),
        teachers=(Teacher("teacher-a", "Ana", ("math",)),),
        subjects=(Subject("math", "Matematică"),),
        rooms=(Room("room-a", "Sala A", 25, ()),),
        time_slots=(
            TimeSlot("mon-1", Weekday.MONDAY, 1, "08:00", "08:50"),
            TimeSlot("tue-1", Weekday.TUESDAY, 1, "08:00", "08:50"),
        ),
        requirements=(
            TeachingRequirement("req-a", "class-a", "math", "teacher-a", 2),
        ),
        constraints=(),
    )


def confirmed_snapshot() -> TimetableProblemSnapshot:
    return TimetableProblemSnapshot(
        snapshot_id="snapshot-a",
        draft_id="draft-a",
        problem_id="problem-a",
        model_id="mock-a",
        version=1,
        original_prompt=PROMPT,
        data=timetable_data(),
    )


class MockAIProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.problem = OriginalProblem("problem-a", PROMPT)
        self.snapshot = confirmed_snapshot()
        self.provider = MockAIProvider(
            "mock-a",
            extraction_responses={PROMPT: EXTRACTION_RESPONSE},
            solution_responses={"snapshot-a": SOLUTION_RESPONSE},
        )

    def test_scripted_extraction_returns_canonical_proposal(self) -> None:
        proposal = self.provider.extract(self.problem)

        self.assertIsInstance(self.provider, AIProviderPort)
        self.assertIsInstance(proposal, ExtractionProposal)
        self.assertTrue(proposal.proposal_id)
        self.assertEqual(proposal.problem_id, "problem-a")
        self.assertEqual(proposal.model_id, "mock-a")
        self.assertEqual(proposal.data, timetable_data())
        self.assertEqual(
            proposal.missing_information,
            (MissingInformation("school.timezone", "Confirmați fusul orar local.", "school.timezone"),),
        )
        self.assertEqual(proposal.unsupported_requirements, ("Supravegherea automată a pauzei de prânz",))
        self.assertEqual(proposal.raw_response, EXTRACTION_RESPONSE)

    def test_extraction_uses_prompt_instead_of_problem_identity(self) -> None:
        proposal = self.provider.extract(OriginalProblem("problem-b", PROMPT))

        self.assertEqual(proposal.problem_id, "problem-b")
        self.assertEqual(proposal.data, timetable_data())

    def test_unknown_or_nearly_matching_prompt_is_rejected_without_echo(self) -> None:
        for prompt in ("private unscripted school data", PROMPT + " "):
            with self.subTest(prompt=prompt):
                with self.assertRaises(ValueError) as caught:
                    self.provider.extract(OriginalProblem("problem-a", prompt))
                self.assertNotIn(prompt, str(caught.exception))

    def test_scripted_solution_returns_canonical_assignments(self) -> None:
        solution = self.provider.solve(self.snapshot)

        self.assertIsInstance(solution, TimetableSolution)
        self.assertTrue(solution.solution_id)
        self.assertEqual(solution.snapshot_id, "snapshot-a")
        self.assertEqual(solution.model_id, "mock-a")
        self.assertEqual(
            solution.assignments,
            (
                LessonAssignment("req-a", 0, "mon-1", "room-a"),
                LessonAssignment("req-a", 1, "tue-1", "room-a"),
            ),
        )

    def test_unknown_snapshot_is_rejected_without_reusing_matching_data(self) -> None:
        for snapshot_id in ("private-unscripted-snapshot", "snapshot-a "):
            with self.subTest(snapshot_id=snapshot_id):
                with self.assertRaises(ValueError) as caught:
                    self.provider.solve(replace(self.snapshot, snapshot_id=snapshot_id))
                self.assertNotIn(snapshot_id, str(caught.exception))

    def test_other_model_cannot_use_a_scripted_snapshot(self) -> None:
        with self.assertRaises(AIResponseError):
            self.provider.solve(replace(self.snapshot, model_id="mock-b"))

    def test_model_id_cannot_be_reassigned(self) -> None:
        with self.assertRaises(AttributeError):
            self.provider.model_id = "mock-b"

        self.assertEqual(self.provider.extract(self.problem).model_id, "mock-a")

    def test_malformed_extraction_script_uses_real_response_error(self) -> None:
        provider = MockAIProvider(
            "mock-a",
            extraction_responses={PROMPT: '{"private-school-name":'},
            solution_responses={},
        )

        with self.assertRaises(AIResponseError) as caught:
            provider.extract(self.problem)
        self.assertNotIn("private-school-name", str(caught.exception))

    def test_malformed_solution_script_uses_real_response_error(self) -> None:
        provider = MockAIProvider(
            "mock-a",
            extraction_responses={},
            solution_responses={"snapshot-a": '{"private-school-name":'},
        )

        with self.assertRaises(AIResponseError) as caught:
            provider.solve(self.snapshot)
        self.assertNotIn("private-school-name", str(caught.exception))

    def test_extraction_scripts_are_copied_on_construction(self) -> None:
        scripts = {PROMPT: EXTRACTION_RESPONSE}
        provider = MockAIProvider("mock-a", extraction_responses=scripts, solution_responses={})
        scripts[PROMPT] = "malformed replacement"
        scripts["new prompt"] = EXTRACTION_RESPONSE

        self.assertEqual(provider.extract(self.problem).data, timetable_data())
        with self.assertRaises(ValueError):
            provider.extract(OriginalProblem("problem-b", "new prompt"))

    def test_solution_scripts_are_copied_on_construction(self) -> None:
        scripts = {"snapshot-a": SOLUTION_RESPONSE}
        provider = MockAIProvider("mock-a", extraction_responses={}, solution_responses=scripts)
        scripts["snapshot-a"] = "malformed replacement"
        scripts["snapshot-b"] = SOLUTION_RESPONSE

        self.assertEqual(
            provider.solve(self.snapshot).assignments,
            (
                LessonAssignment("req-a", 0, "mon-1", "room-a"),
                LessonAssignment("req-a", 1, "tue-1", "room-a"),
            ),
        )
        with self.assertRaises(ValueError):
            provider.solve(replace(self.snapshot, snapshot_id="snapshot-b"))

    def test_scripts_do_not_leak_between_provider_instances(self) -> None:
        other_provider = MockAIProvider("mock-b", extraction_responses={}, solution_responses={})

        with self.assertRaises(ValueError):
            other_provider.extract(self.problem)
        with self.assertRaises(ValueError):
            other_provider.solve(replace(self.snapshot, model_id="mock-b"))

    def test_solve_preserves_the_confirmed_snapshot(self) -> None:
        original_value = deepcopy(self.snapshot)
        original_data = self.snapshot.data

        self.provider.solve(self.snapshot)

        self.assertEqual(self.snapshot, original_value)
        self.assertIs(self.snapshot.data, original_data)

    def test_invalid_scripted_solution_is_preserved_for_independent_verification(self) -> None:
        provider = MockAIProvider(
            "mock-a",
            extraction_responses={},
            solution_responses={"snapshot-a": INVALID_SOLUTION_RESPONSE},
        )

        solution = provider.solve(self.snapshot)
        result = TimetableVerifier().verify(self.snapshot, solution)

        self.assertEqual(
            solution.assignments,
            (
                LessonAssignment("req-a", 0, "mon-1", "room-a"),
                LessonAssignment("req-a", 1, "mon-1", "room-a"),
            ),
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("schedule.teacher_overlap", {item.code for item in result.violations})

    def test_blank_or_non_string_model_identifier_is_rejected(self) -> None:
        for model_id in ("", "  \t", None, 12, True):
            with self.subTest(model_id=model_id):
                with self.assertRaises(ValueError):
                    MockAIProvider(model_id, extraction_responses={}, solution_responses={})

    def test_scripts_require_string_keys_and_string_responses(self) -> None:
        for scripts in ({12: EXTRACTION_RESPONSE}, {PROMPT: 12}, {PROMPT: None}, []):
            with self.subTest(scripts=scripts):
                with self.assertRaises(ValueError):
                    MockAIProvider("mock-a", extraction_responses=scripts, solution_responses={})
                with self.assertRaises(ValueError):
                    MockAIProvider("mock-a", extraction_responses={}, solution_responses=scripts)


if __name__ == "__main__":
    unittest.main()
