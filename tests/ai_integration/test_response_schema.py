"""Wire-schema contract checks for AI extraction and solving requests."""

from importlib import import_module, util
import json
import unittest


class ResponseSchemaTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.response_schema"
        self.assertIsNotNone(util.find_spec(name), "The AI output schema must exist")
        self.schemas = import_module(name)

    def test_extraction_requests_domain_data_without_workflow_identifiers(self):
        schema = self.schemas.extraction_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {
            "data", "missing_information", "unsupported_requirements",
        })
        self.assertEqual(set(schema["properties"]), {
            "data", "missing_information", "unsupported_requirements",
        })
        self.assertEqual(schema["properties"]["unsupported_requirements"], {
            "type": "array", "items": {"type": "string"},
        })
        self.assertEqual(json.loads(json.dumps(schema)), schema)

    def test_timetable_entity_schema_matches_canonical_types_and_unknown_values(self):
        data = self.schemas.extraction_json_schema()["properties"]["data"]
        self.assertEqual(set(data["required"]), {
            "classes", "teachers", "subjects", "rooms", "time_slots", "requirements", "constraints",
        })
        school_class = data["properties"]["classes"]["items"]
        self.assertEqual(school_class["properties"]["student_count"], {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
        })
        self.assertEqual(school_class["properties"]["class_id"], {"type": "string"})
        slot = data["properties"]["time_slots"]["items"]
        self.assertEqual(slot["properties"]["day"], {
            "type": "string", "enum": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        })
        self.assertEqual(slot["properties"]["period"], {"type": "integer"})
        self.assertEqual(slot["properties"]["start_time"], {
            "anyOf": [{"type": "string"}, {"type": "null"}],
        })

    def test_constraint_branches_discriminate_same_shaped_parameters_by_type(self):
        schema = self.schemas.extraction_json_schema()
        branches = schema["properties"]["data"]["properties"]["constraints"]["items"]["anyOf"]
        expected_fields = {
            "teacher_unavailable": {"teacher_id", "slot_ids"},
            "room_unavailable": {"room_id", "slot_ids"},
            "class_unavailable": {"class_id", "slot_ids"},
            "room_feature_required": {"requirement_id", "feature"},
            "max_class_periods_per_day": {"class_id", "maximum"},
            "teacher_preferred_slots": {"teacher_id", "slot_ids"},
            "minimize_teacher_gaps": {"teacher_id"},
            "minimize_class_gaps": {"class_id"},
            "balance_class_daily_load": {"class_id"},
        }
        self.assertEqual(len(branches), 9)
        actual = {}
        for branch in branches:
            tag = branch["properties"]["type"]["enum"]
            self.assertEqual(len(tag), 1)
            parameters = branch["properties"]["parameters"]
            actual[tag[0]] = set(parameters["properties"])
            self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
            self.assertFalse(parameters["additionalProperties"])
            self.assertEqual(branch["properties"]["severity"]["enum"], ["hard", "soft"])
        self.assertEqual(actual, expected_fields)

    def test_all_nested_objects_require_their_declared_fields_and_forbid_extras(self):
        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(self.schemas.extraction_json_schema())
        check(self.schemas.solution_json_schema())

    def test_solution_schema_requests_assignments_only(self):
        schema = self.schemas.solution_json_schema()
        self.assertEqual(set(schema["properties"]), {"assignments"})
        self.assertEqual(schema["required"], ["assignments"])
        assignments = schema["properties"]["assignments"]
        self.assertEqual(assignments["type"], "array")
        self.assertEqual(assignments["items"]["properties"], {
            "requirement_id": {"type": "string"}, "occurrence": {"type": "integer"},
            "slot_id": {"type": "string"}, "room_id": {"type": "string"},
        })
        # Unknown references, missing lessons and negative occurrences are
        # mathematical findings, not shape-based reasons to discard an attempt.
        self.assertNotIn("minItems", assignments)
        self.assertNotIn("minimum", assignments["items"]["properties"]["occurrence"])

    def test_schema_mutation_cannot_affect_another_invocation(self):
        schema = self.schemas.extraction_json_schema()
        schema["properties"]["data"]["properties"]["classes"]["items"]["properties"].clear()
        fresh = self.schemas.extraction_json_schema()
        self.assertIn("class_id", fresh["properties"]["data"]["properties"]["classes"]["items"]["properties"])
        first = self.schemas.solution_json_schema()
        first["properties"]["assignments"]["items"]["properties"]["occurrence"]["type"] = "boolean"
        self.assertEqual(
            self.schemas.solution_json_schema()["properties"]["assignments"]["items"]["properties"]["occurrence"],
            {"type": "integer"},
        )


if __name__ == "__main__":
    unittest.main()
