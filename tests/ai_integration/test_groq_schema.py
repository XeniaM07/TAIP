"""Preserve schema meaning while avoiding Groq's ambiguous enum discriminator."""

from copy import deepcopy
import re
import unittest

from taip.ai_integration import response_schema


class GroqSchemaTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(response_schema, "groq_response_schema"), "Groq requires a compatible encoding of the canonical schema")

    def test_constraint_type_is_the_only_direct_enum_discriminator(self):
        adapted = response_schema.groq_response_schema(response_schema.extraction_json_schema())
        branches = adapted["properties"]["data"]["properties"]["constraints"]["items"]["anyOf"]
        self.assertEqual(len(branches), 9)
        for branch in branches:
            direct_enums = {name for name, schema in branch["properties"].items() if "enum" in schema}
            self.assertEqual(direct_enums, {"type"})
            self.assertEqual(len(branch["properties"]["type"]["enum"]), 1)

    def test_severity_keeps_exactly_the_same_accepted_values(self):
        original = response_schema.extraction_json_schema()
        adapted = response_schema.groq_response_schema(original)
        branches = adapted["properties"]["data"]["properties"]["constraints"]["items"]["anyOf"]
        for branch in branches:
            schema = branch["properties"]["severity"]
            for value in ("hard", "soft", "HARD", "hard\n", "soft\r", "hardsoft", "", " hard", "hard ", "har\u200bd", None, 1, True):
                accepted = (
                    isinstance(value, str)
                    and schema["minLength"] <= len(value) <= schema["maxLength"]
                    and re.search(schema["pattern"], value) is not None
                )
                self.assertEqual(accepted, value in ("hard", "soft"), repr(value))

    def test_all_other_types_parameters_and_closed_fields_are_unchanged(self):
        original = response_schema.extraction_json_schema()
        adapted = response_schema.groq_response_schema(original)
        branches = adapted["properties"]["data"]["properties"]["constraints"]["items"]["anyOf"]
        originals = original["properties"]["data"]["properties"]["constraints"]["items"]["anyOf"]
        for old, new in zip(originals, branches, strict=True):
            self.assertFalse(new["additionalProperties"])
            self.assertEqual(new["required"], old["required"])
            new["properties"]["severity"] = deepcopy(old["properties"]["severity"])
        self.assertEqual(adapted, original)

    def test_adapter_never_mutates_input_and_solution_schema_needs_no_change(self):
        original = response_schema.extraction_json_schema()
        before = deepcopy(original)
        adapted = response_schema.groq_response_schema(original)
        adapted["properties"]["data"]["properties"]["classes"]["items"]["properties"].clear()
        self.assertEqual(original, before)
        solution = response_schema.solution_json_schema()
        adapted_solution = response_schema.groq_response_schema(solution)
        self.assertEqual(adapted_solution, solution)
        adapted_solution["properties"].clear()
        self.assertIn("assignments", solution["properties"])


if __name__ == "__main__":
    unittest.main()
