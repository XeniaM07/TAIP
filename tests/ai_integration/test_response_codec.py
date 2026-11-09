"""Behavior checks for the untrusted AI JSON boundary."""

from dataclasses import FrozenInstanceError
from importlib import import_module, util
import json
import unittest

from taip.common.timetable_contracts import (
    BalanceClassDailyLoad,
    ClassUnavailable,
    ConstraintSeverity,
    ExtractionProposal,
    MaxClassPeriodsPerDay,
    MinimizeClassGaps,
    MinimizeTeacherGaps,
    OriginalProblem,
    LessonAssignment,
    RoomFeatureRequired,
    RoomUnavailable,
    TeacherPreferredSlots,
    TeacherUnavailable,
    TimetableData,
    TimetableProblemSnapshot,
    TimetableSolution,
    Weekday,
)


def extraction_payload():
    """Literal provider output, independent of production serialization."""
    return {
        "data": {
            "classes": [{"class_id": "5A", "name": "Clasa a V-a A", "student_count": 25}],
            "teachers": [{"teacher_id": "ana", "name": "Ana", "subject_ids": ["math"]}],
            "subjects": [{"subject_id": "math", "name": "Matematică"}],
            "rooms": [{"room_id": "r1", "name": "Sala 1", "capacity": 30, "features": []}],
            "time_slots": [{"slot_id": "mon-1", "day": "monday", "period": 1}],
            "requirements": [{
                "requirement_id": "req-1", "class_id": "5A", "subject_id": "math",
                "teacher_id": "ana", "sessions_per_week": 1,
            }],
            "constraints": [],
        },
        "missing_information": [],
        "unsupported_requirements": [],
    }


class ExtractionResponseCodecTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.response_codec"
        self.assertIsNotNone(util.find_spec(name), "The AI response codec must exist")
        self.codec = import_module(name)
        self.error = import_module("taip.ai_integration.errors").AIResponseError
        self.problem = OriginalProblem("problem-1", "Clasa a V-a A are matematică cu Ana.")

    def decode(self, payload):
        return self.codec.parse_extraction_response(
            json.dumps(payload, ensure_ascii=False), problem=self.problem, model_id="gemini"
        )

    def test_decodes_canonical_contracts_and_preserves_exact_raw_response(self):
        raw = "\n " + json.dumps(extraction_payload(), ensure_ascii=False) + " \n"
        proposal = self.codec.parse_extraction_response(
            raw, problem=self.problem, model_id="gemini"
        )
        self.assertIsInstance(proposal, ExtractionProposal)
        self.assertEqual(proposal.problem_id, "problem-1")
        self.assertEqual(proposal.model_id, "gemini")
        self.assertTrue(proposal.proposal_id)
        self.assertEqual(proposal.raw_response, raw)
        self.assertEqual(proposal.data.classes[0].student_count, 25)
        self.assertEqual(proposal.data.teachers[0].subject_ids, ("math",))
        self.assertIs(proposal.data.time_slots[0].day, Weekday.MONDAY)
        self.assertIsInstance(proposal.data.rooms, tuple)
        with self.assertRaises(FrozenInstanceError):
            proposal.data.classes[0].name = "Changed"

    def test_partial_extraction_retains_missing_and_unsupported_information(self):
        proposal = self.decode({
            "data": {"classes": [{"class_id": "5A", "name": "Clasa a V-a A"}]},
            "missing_information": [{
                "code": "TEACHER_MISSING", "message": "Profesorul nu este precizat.",
                "path": "data.teachers",
            }],
            "unsupported_requirements": ["Săptămâni alternative"],
        })
        self.assertEqual(proposal.data.teachers, ())
        self.assertEqual(proposal.data.requirements, ())
        self.assertIsNone(proposal.data.classes[0].student_count)
        self.assertEqual(proposal.missing_information[0].code, "TEACHER_MISSING")
        self.assertEqual(proposal.missing_information[0].path, "data.teachers")
        self.assertEqual(proposal.unsupported_requirements, ("Săptămâni alternative",))

    def test_empty_data_does_not_invent_domain_entities(self):
        proposal = self.decode({"data": {}})
        self.assertEqual(proposal.data.classes, ())
        self.assertEqual(proposal.data.teachers, ())
        self.assertEqual(proposal.data.rooms, ())
        self.assertEqual(proposal.data.time_slots, ())
        self.assertEqual(proposal.data.requirements, ())

    def test_constraint_type_selects_its_exact_parameter_contract(self):
        cases = (
            ("teacher_unavailable", {"teacher_id": "ana", "slot_ids": ["mon-1"]}, TeacherUnavailable),
            ("room_unavailable", {"room_id": "r1", "slot_ids": ["mon-1"]}, RoomUnavailable),
            ("class_unavailable", {"class_id": "5A", "slot_ids": ["mon-1"]}, ClassUnavailable),
            ("room_feature_required", {"requirement_id": "req-1", "feature": "computers"}, RoomFeatureRequired),
            ("max_class_periods_per_day", {"class_id": "5A", "maximum": 4}, MaxClassPeriodsPerDay),
            ("teacher_preferred_slots", {"teacher_id": "ana", "slot_ids": ["mon-1"]}, TeacherPreferredSlots),
            ("minimize_teacher_gaps", {"teacher_id": None}, MinimizeTeacherGaps),
            ("minimize_class_gaps", {"class_id": None}, MinimizeClassGaps),
            ("balance_class_daily_load", {"class_id": None}, BalanceClassDailyLoad),
        )
        for kind, parameters, expected in cases:
            with self.subTest(kind=kind):
                payload = extraction_payload()
                payload["data"]["constraints"] = [{
                    "constraint_id": "c1", "type": kind, "severity": "soft",
                    "parameters": parameters, "weight": 2, "source_text": "Regula explicită.",
                }]
                constraint = self.decode(payload).data.constraints[0]
                self.assertIs(type(constraint.parameters), expected)
                self.assertIs(constraint.severity, ConstraintSeverity.SOFT)
                self.assertEqual(constraint.weight, 2)
                self.assertEqual(constraint.source_text, "Regula explicită.")
                if "slot_ids" in parameters:
                    self.assertEqual(constraint.parameters.slot_ids, ("mon-1",))

    def test_rejects_constraint_parameters_for_a_different_constraint_type(self):
        payload = extraction_payload()
        payload["data"]["constraints"] = [{
            "constraint_id": "c1", "type": "room_unavailable", "severity": "hard",
            "parameters": {"teacher_id": "ana", "slot_ids": ["mon-1"]},
        }]
        with self.assertRaises(self.error) as caught:
            self.decode(payload)
        self.assertEqual(caught.exception.code, "invalid_schema")
        self.assertIn("parameters", caught.exception.path)

    def test_rejects_bool_float_and_string_coercion_into_integer_fields(self):
        for value in (True, 25.0, "25"):
            with self.subTest(value=value):
                payload = extraction_payload()
                payload["data"]["classes"][0]["student_count"] = value
                with self.assertRaises(self.error) as caught:
                    self.decode(payload)
                self.assertEqual(caught.exception.path, "$.data.classes[0].student_count")

    def test_rejects_unknown_nested_fields_without_exposing_their_values(self):
        payload = extraction_payload()
        payload["data"]["rooms"][0]["unexpected"] = "PRIVATE_PROVIDER_BODY"
        with self.assertRaises(self.error) as caught:
            self.decode(payload)
        self.assertEqual(caught.exception.code, "invalid_schema")
        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(caught.exception))

    def test_rejects_missing_required_fields_with_a_precise_path(self):
        payload = extraction_payload()
        del payload["data"]["requirements"][0]["teacher_id"]
        with self.assertRaises(self.error) as caught:
            self.decode(payload)
        self.assertEqual(caught.exception.path, "$.data.requirements[0].teacher_id")

    def test_rejects_wrong_collection_enum_and_nested_scalar_shapes(self):
        mutations = (
            ("teachers", 0, "subject_ids", "math"),
            ("teachers", 0, "name", None),
            ("time_slots", 0, "day", "luni"),
            ("rooms", 0, "features", [42]),
        )
        for collection, index, field, value in mutations:
            with self.subTest(field=field):
                payload = extraction_payload()
                payload["data"][collection][index][field] = value
                with self.assertRaises(self.error) as caught:
                    self.decode(payload)
                self.assertEqual(caught.exception.code, "invalid_schema")

    def test_rejects_workflow_identity_fields_supplied_by_the_model(self):
        for field in ("problem_id", "model_id", "proposal_id", "raw_response"):
            with self.subTest(field=field):
                payload = extraction_payload()
                payload[field] = "untrusted-value"
                with self.assertRaises(self.error):
                    self.decode(payload)

    def test_rejects_invalid_json_instead_of_repairing_it(self):
        cases = (
            "", "```json\n{\"data\":{}}\n```", '{"data":',
            'Explanation: {"data":{}}', '{"data":{}} trailing',
            '{"data":{},"data":{}}',
            '{"data":{"classes":[{"class_id":"a","class_id":"b","name":"A"}]}}',
            '{"data":{},"missing_information":NaN}',
            '{"data":{},"missing_information":Infinity}',
        )
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(self.error) as caught:
                    self.codec.parse_extraction_response(raw, problem=self.problem, model_id="gemini")
                self.assertEqual(caught.exception.code, "invalid_json")

    def test_rejects_valid_json_with_an_invalid_top_level_schema(self):
        for payload in (None, [], "text", 1, {}, {"data": []}):
            with self.subTest(payload=payload):
                with self.assertRaises(self.error) as caught:
                    self.decode(payload)
                self.assertEqual(caught.exception.code, "invalid_schema")

    def test_rejects_non_text_responses_as_typed_errors(self):
        for raw in (None, b'{"data":{}}'):
            with self.subTest(raw=raw):
                with self.assertRaises(self.error) as caught:
                    self.codec.parse_extraction_response(raw, problem=self.problem, model_id="gemini")
                self.assertEqual(caught.exception.code, "invalid_json")

    def test_error_retains_raw_output_without_putting_it_in_the_message(self):
        raw = "PRIVATE_PROVIDER_BODY: not JSON"
        with self.assertRaises(self.error) as caught:
            self.codec.parse_extraction_response(raw, problem=self.problem, model_id="gemini")
        self.assertEqual(caught.exception.raw_response, raw)
        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(caught.exception))

    def test_extraction_attempts_receive_distinct_local_result_ids(self):
        first = self.decode({"data": {}})
        second = self.decode({"data": {}})
        self.assertNotEqual(first.proposal_id, second.proposal_id)


class SolutionResponseCodecTests(unittest.TestCase):
    def setUp(self):
        self.codec = import_module("taip.ai_integration.response_codec")
        self.assertTrue(
            hasattr(self.codec, "parse_solution_response"), "The solving boundary must exist"
        )
        self.error = import_module("taip.ai_integration.errors").AIResponseError
        self.snapshot = TimetableProblemSnapshot(
            "snapshot-1", "draft-1", "problem-1", "gemini", 1, "Problema confirmată.",
            TimetableData(),
        )

    def decode(self, payload, model_id="gemini"):
        return self.codec.parse_solution_response(
            json.dumps(payload), snapshot=self.snapshot, model_id=model_id
        )

    def test_assignments_use_canonical_contracts_and_trusted_workflow_identity(self):
        solution = self.decode({"assignments": [{
            "requirement_id": "req-1", "occurrence": 0, "slot_id": "mon-1", "room_id": "r1",
        }]})
        self.assertIsInstance(solution, TimetableSolution)
        self.assertEqual(solution.snapshot_id, "snapshot-1")
        self.assertEqual(solution.model_id, "gemini")
        self.assertEqual(solution.assignments, (LessonAssignment("req-1", 0, "mon-1", "r1"),))
        self.assertTrue(solution.solution_id)

    def test_preserves_duplicate_and_unknown_assignments_for_the_verifier(self):
        invalid = {"requirement_id": "missing", "occurrence": 42, "slot_id": "unknown", "room_id": "none"}
        solution = self.decode({"assignments": [invalid, invalid]})
        self.assertEqual(solution.assignments, (
            LessonAssignment("missing", 42, "unknown", "none"),
            LessonAssignment("missing", 42, "unknown", "none"),
        ))

    def test_preserves_an_empty_solution_instead_of_inventing_lessons(self):
        self.assertEqual(self.decode({"assignments": []}).assignments, ())

    def test_rejects_a_foreign_model_snapshot_before_returning_a_solution(self):
        with self.assertRaises(self.error) as caught:
            self.decode({"assignments": []}, model_id="qwen")
        self.assertEqual(caught.exception.code, "model_mismatch")

    def test_rejects_workflow_ids_supplied_by_the_model(self):
        for key in ("model_id", "snapshot_id", "solution_id"):
            with self.subTest(key=key):
                with self.assertRaises(self.error):
                    self.decode({"assignments": [], key: "untrusted"})

    def test_rejects_missing_assignments_or_assignment_members(self):
        for payload in ({}, {"assignments": [{"requirement_id": "r"}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(self.error) as caught:
                    self.decode(payload)
                self.assertEqual(caught.exception.code, "invalid_schema")

    def test_rejects_assignment_type_coercion(self):
        for value in (False, "0", 0.0):
            with self.subTest(value=value):
                with self.assertRaises(self.error) as caught:
                    self.decode({"assignments": [{
                        "requirement_id": "r", "occurrence": value, "slot_id": "s", "room_id": "room",
                    }]})
                self.assertEqual(caught.exception.path, "$.assignments[0].occurrence")

    def test_rejects_wrong_assignment_collection_shapes(self):
        for assignments in (None, {}, "lessons", [None], ["lesson"]):
            with self.subTest(assignments=assignments):
                with self.assertRaises(self.error):
                    self.decode({"assignments": assignments})

    def test_preserves_raw_output_when_solving_json_is_invalid(self):
        raw = '{"assignments": PRIVATE_PROVIDER_BODY'
        with self.assertRaises(self.error) as caught:
            self.codec.parse_solution_response(raw, snapshot=self.snapshot, model_id="gemini")
        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertEqual(caught.exception.raw_response, raw)
        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(caught.exception))

    def test_does_not_mutate_snapshot_and_generates_distinct_solution_ids(self):
        original_data = self.snapshot.data
        original_hash = hash(self.snapshot)
        first = self.decode({"assignments": []})
        second = self.decode({"assignments": []})
        self.assertIs(self.snapshot.data, original_data)
        self.assertEqual(hash(self.snapshot), original_hash)
        self.assertNotEqual(first.solution_id, second.solution_id)


if __name__ == "__main__":
    unittest.main()
