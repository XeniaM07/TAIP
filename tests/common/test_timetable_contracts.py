"""Regression tests for the version-1 timetabling fixture schema."""
import json
import unittest
from pathlib import Path
from taip.common.timetable_contracts import ConstraintType, TimetableProblemSnapshot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "timetable"

class TimetableContractsFixtureTests(unittest.TestCase):
    def load(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_all_seven_fixtures_exist_and_parse(self):
        for name in ("original_problem.json", "extraction_ai1.json", "extraction_ai2.json", "extraction_ai3.json", "confirmed_snapshot.json", "valid_solution.json", "invalid_solution.json"):
            with self.subTest(name=name):
                self.assertIsInstance(self.load(name), dict)

    def test_proposals_are_model_specific(self):
        models = [self.load(f"extraction_ai{i}.json")["model_id"] for i in (1, 2, 3)]
        self.assertEqual(len(set(models)), 3)

    def test_snapshot_has_required_contract_fields(self):
        snapshot = self.load("confirmed_snapshot.json")
        self.assertEqual(set(TimetableProblemSnapshot.__dataclass_fields__), set(snapshot))
        self.assertGreater(snapshot["version"], 0)
        self.assertTrue(snapshot["data"]["rooms"])

    def test_constraint_types_are_supported(self):
        snapshot = self.load("confirmed_snapshot.json")
        for constraint in snapshot["data"]["constraints"]:
            self.assertIn(constraint["type"], {t.value for t in ConstraintType})

    def test_valid_solution_has_all_occurrences(self):
        snapshot = self.load("confirmed_snapshot.json")
        solution = self.load("valid_solution.json")
        expected = {(r["requirement_id"], i) for r in snapshot["data"]["requirements"] for i in range(r["sessions_per_week"])}
        actual = {(a["requirement_id"], a["occurrence"]) for a in solution["assignments"]}
        self.assertEqual(expected, actual)
        self.assertEqual(len(expected), len(solution["assignments"]))

    def test_valid_solution_has_no_resource_conflicts(self):
        snapshot = self.load("confirmed_snapshot.json")
        solution = self.load("valid_solution.json")
        reqs = {r["requirement_id"]: r for r in snapshot["data"]["requirements"]}
        seen = set()
        for a in solution["assignments"]:
            r = reqs[a["requirement_id"]]
            for resource in (("class", r["class_id"]), ("teacher", r["teacher_id"]), ("room", a["room_id"])):
                key = (a["slot_id"], *resource)
                self.assertNotIn(key, seen)
                seen.add(key)

    def test_invalid_solution_has_room_conflict(self):
        solution = self.load("invalid_solution.json")
        rooms = [(a["slot_id"], a["room_id"]) for a in solution["assignments"]]
        self.assertLess(len(set(rooms)), len(rooms))

if __name__ == "__main__":
    unittest.main()
