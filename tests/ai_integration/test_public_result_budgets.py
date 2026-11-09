"""Supported public result sizes stay below the session response budget."""

import json
import unittest

from taip.ai_integration.errors import AIResponseError
from taip.ai_integration.response_codec import decode_timetable_data, parse_extraction_response, parse_solution_response
from taip.common.timetable_contracts import OriginalProblem, TimetableData, TimetableProblemSnapshot


class PublicResultBudgetTests(unittest.TestCase):
    def test_auxiliary_proposal_text_is_bounded_and_private_raw_is_retained(self):
        raw = json.dumps({"data": {}, "missing_information": [{"code": "missing", "message": "x" * 140000}]})
        with self.assertRaises(AIResponseError) as caught:
            parse_extraction_response(raw, problem=OriginalProblem("p", "prompt"), model_id="m")
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertEqual(caught.exception.raw_response, raw)

    def test_solution_public_data_is_bounded_without_losing_original_invalid_output(self):
        raw = json.dumps({"assignments": [{"requirement_id": "x" * 270000, "occurrence": 0, "slot_id": "s", "room_id": "r"}]})
        snapshot = TimetableProblemSnapshot("s", "d", "p", "m", 1, "prompt", TimetableData())
        with self.assertRaises(AIResponseError) as caught:
            parse_solution_response(raw, snapshot=snapshot, model_id="m")
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertEqual(caught.exception.raw_response, raw)

    def test_domain_budget_counts_json_structure_as_well_as_scalar_text(self):
        data = {"rooms": [{"room_id": str(index), "name": "x" * 40} for index in range(1000)]}
        self.assertGreater(len(json.dumps(data, separators=(",", ":")).encode()), 65536)
        with self.assertRaises(AIResponseError):
            decode_timetable_data(data)
