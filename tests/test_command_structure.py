
"""Structural tests for the Command design pattern."""

import inspect
import unittest

from taip.problem_management.commands import (
    AddConstraintCommand,
    ConstraintCommand,
    RemoveConstraintCommand,
    UpdateConstraintCommand,
)
from taip.problem_management.editor import ProblemEditor
from taip.problem_management.problem import MathematicalProblem


class CommandStructureTests(unittest.TestCase):

    def test_command_is_abstract(self):
        self.assertTrue(inspect.isabstract(ConstraintCommand))

    def test_concrete_commands_inherit_from_command(self):
        for command_type in (
            AddConstraintCommand,
            UpdateConstraintCommand,
            RemoveConstraintCommand,
        ):
            with self.subTest(command=command_type.__name__):
                self.assertTrue(issubclass(command_type, ConstraintCommand))
                self.assertFalse(inspect.isabstract(command_type))

    def test_commands_reference_receiver(self):
        for command_type in (
            AddConstraintCommand,
            UpdateConstraintCommand,
            RemoveConstraintCommand,
        ):
            with self.subTest(command=command_type.__name__):
                self.assertIn("problem", command_type.__annotations__)

    def test_receiver_declares_constraint_operations(self):
        for method in (
            "add_constraint",
            "update_constraint",
            "remove_constraint",
        ):
            self.assertTrue(callable(getattr(MathematicalProblem, method)))

    def test_invoker_uses_command_interface(self):
        self.assertIn("execute_command", ProblemEditor.__dict__)


if __name__ == "__main__":
    unittest.main()
