"""Structural tests for the Memento design pattern."""

import unittest
from dataclasses import FrozenInstanceError, is_dataclass

from taip.common.contracts import Constraint, ProblemSnapshot
from taip.problem_management.caretaker import ProblemCaretaker
from taip.problem_management.memento import ProblemMemento
from taip.problem_management.problem import MathematicalProblem


class MementoStructureTests(unittest.TestCase):

    def test_memento_is_immutable(self):
        memento = ProblemMemento(
            _problem_id="p1",
            _objective="Find x",
            _constraints=(Constraint("c1", "x > 0"),),
        )

        self.assertTrue(is_dataclass(memento))

        with self.assertRaises(FrozenInstanceError):
            memento._objective = "Changed"

    def test_originator_declares_memento_operations(self):
        self.assertTrue(
            callable(getattr(MathematicalProblem, "create_memento"))
        )
        self.assertTrue(
            callable(getattr(MathematicalProblem, "restore"))
        )

    def test_caretaker_declares_storage_operations(self):
        self.assertTrue(callable(getattr(ProblemCaretaker, "save")))
        self.assertTrue(
            callable(getattr(ProblemCaretaker, "get_memento"))
        )

    def test_caretaker_declares_memento_collection(self):
        self.assertIn("mementos", ProblemCaretaker.__annotations__)

    def test_confirmed_snapshot_remains_separate(self):
        self.assertIsNot(ProblemMemento, ProblemSnapshot)


if __name__ == "__main__":
    unittest.main()