
"""Functional TDD tests for the Command pattern."""

import unittest

from taip.common.contracts import Constraint
from taip.problem_management.commands import (
    AddConstraintCommand,
    UpdateConstraintCommand,
    RemoveConstraintCommand,
)
from taip.problem_management.editor import ProblemEditor
from taip.problem_management.problem import MathematicalProblem


class ConstraintEditingTDDTests(unittest.TestCase):

    def setUp(self):
        self.problem = MathematicalProblem()
        self.problem.problem_id = "problem-1"
        self.problem.original_prompt = "Solve x + 2 = 5"
        self.problem.objective = "Find x"
        self.problem.constraints = []
        self.editor = ProblemEditor()

    def test_add_constraint(self):
        constraint = Constraint("c1", "x >= 0")

        command = AddConstraintCommand()
        command.problem = self.problem
        command.constraint = constraint

        self.editor.execute_command(command)

        self.assertEqual(
            self.problem.constraints,
            [constraint],
        )

    def test_update_constraint(self):
        self.problem.constraints = [
            Constraint("c1", "x >= 0")
        ]

        command = UpdateConstraintCommand()
        command.problem = self.problem
        command.constraint = Constraint("c1", "x > 2")

        self.editor.execute_command(command)

        self.assertEqual(
            self.problem.constraints,
            [Constraint("c1", "x > 2")],
        )

    def test_remove_constraint(self):
        self.problem.constraints = [
            Constraint("c1", "x >= 0")
        ]

        command = RemoveConstraintCommand()
        command.problem = self.problem
        command.constraint_id = "c1"

        self.editor.execute_command(command)

        self.assertEqual(self.problem.constraints, [])

    def test_duplicate_constraint_is_rejected(self):
        self.problem.constraints = [
            Constraint("c1", "x >= 0")
        ]

        command = AddConstraintCommand()
        command.problem = self.problem
        command.constraint = Constraint("c1", "x > 2")

        with self.assertRaises(ValueError):
            self.editor.execute_command(command)

    def test_update_missing_constraint_is_rejected(self):
        command = UpdateConstraintCommand()
        command.problem = self.problem
        command.constraint = Constraint("unknown", "x > 2")

        with self.assertRaises(ValueError):
            self.editor.execute_command(command)

    def test_remove_missing_constraint_is_rejected(self):
        command = RemoveConstraintCommand()
        command.problem = self.problem
        command.constraint_id = "unknown"

        with self.assertRaises(ValueError):
            self.editor.execute_command(command)


if __name__ == "__main__":
    unittest.main()
