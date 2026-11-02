
"""TDD tests for problem confirmation and version history."""

import unittest
from dataclasses import FrozenInstanceError

from taip.common.contracts import Constraint, ProblemSnapshot
from taip.problem_management.problem import MathematicalProblem
from taip.problem_management.version_history import ProblemVersionHistory


class ProblemConfirmationTDDTests(unittest.TestCase):

    def setUp(self):
        self.problem = MathematicalProblem()
        self.problem.problem_id = "problem-1"
        self.problem.original_prompt = "Solve x + 2 = 5"
        self.problem.objective = "Find x"
        self.problem.constraints = [
            Constraint("c1", "x >= 0")
        ]
        self.problem.version_history = ProblemVersionHistory()

    def test_confirm_creates_snapshot(self):
        snapshot = self.problem.confirm(version=1)

        self.assertIsInstance(snapshot, ProblemSnapshot)
        self.assertEqual(snapshot.problem_id, "problem-1")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.objective, "Find x")
        self.assertEqual(
            snapshot.constraints,
            (Constraint("c1", "x >= 0"),),
        )

    def test_confirm_stores_snapshot_in_history(self):
        snapshot = self.problem.confirm(version=1)

        stored = self.problem.version_history.get_snapshot(
            "problem-1", 1
        )

        self.assertEqual(stored, snapshot)

    def test_snapshot_remains_unchanged_after_editing(self):
        snapshot = self.problem.confirm(version=1)

        self.problem.objective = "Maximize x"
        self.problem.add_constraint(
            Constraint("c2", "x <= 10")
        )

        self.assertEqual(snapshot.objective, "Find x")
        self.assertEqual(len(snapshot.constraints), 1)

    def test_snapshot_is_immutable(self):
        snapshot = self.problem.confirm(version=1)

        with self.assertRaises(FrozenInstanceError):
            snapshot.objective = "Changed"

    def test_reject_duplicate_version(self):
        self.problem.confirm(version=1)

        with self.assertRaises(ValueError):
            self.problem.confirm(version=1)

    def test_reject_invalid_version(self):
        with self.assertRaises(ValueError):
            self.problem.confirm(version=0)

    def test_retrieve_missing_version(self):
        with self.assertRaises(KeyError):
            self.problem.version_history.get_snapshot(
                "problem-1", 99
            )

    def test_confirm_multiple_versions(self):
        first = self.problem.confirm(version=1)

        self.problem.objective = "Minimize x"
        second = self.problem.confirm(version=2)

        self.assertEqual(first.objective, "Find x")
        self.assertEqual(second.objective, "Minimize x")
        self.assertEqual(
            self.problem.version_history.get_snapshot(
                "problem-1", 1
            ),
            first,
        )


if __name__ == "__main__":
    unittest.main()
