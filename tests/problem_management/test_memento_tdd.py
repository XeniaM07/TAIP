
"""Functional TDD tests for the Memento pattern."""

import unittest

from dataclasses import FrozenInstanceError

from taip.common.contracts import Constraint
from taip.problem_management.problem import MathematicalProblem
from taip.problem_management.caretaker import ProblemCaretaker


class MementoTDDTests(unittest.TestCase):

    def setUp(self):
        self.problem = MathematicalProblem()
        self.problem.problem_id = "problem-1"
        self.problem.original_prompt = "Solve x + 2 = 5"
        self.problem.objective = "Find x"
        self.problem.constraints = [
            Constraint("c1", "x >= 0")
        ]
        self.caretaker = ProblemCaretaker()

    def test_save_and_restore_problem_state(self):
        original_state = self.problem.create_memento()
        self.caretaker.save(original_state)

        self.problem.objective = "Find maximum x"
        self.problem.add_constraint(
            Constraint("c2", "x <= 10")
        )

        saved_state = self.caretaker.get_memento(0)
        self.problem.restore(saved_state)

        self.assertEqual(self.problem.objective, "Find x")
        self.assertEqual(
            self.problem.constraints,
            [Constraint("c1", "x >= 0")],
        )

    def test_restore_original_prompt(self):
        original_state = self.problem.create_memento()

        self.problem.original_prompt = "Changed problem"
        self.problem.restore(original_state)

        self.assertEqual(
            self.problem.original_prompt,
            "Solve x + 2 = 5",
        )

    def test_memento_is_immutable(self):
        state = self.problem.create_memento()

        with self.assertRaises(FrozenInstanceError):
            state._objective = "Changed"

    def test_saved_state_is_independent_of_later_changes(self):
        state = self.problem.create_memento()

        self.problem.constraints.append(
            Constraint("c2", "x <= 10")
        )

        self.assertEqual(
            state._constraints,
            (Constraint("c1", "x >= 0"),),
        )

    def test_multiple_saved_states(self):
        first_state = self.problem.create_memento()
        self.caretaker.save(first_state)

        self.problem.objective = "Minimize x"

        second_state = self.problem.create_memento()
        self.caretaker.save(second_state)

        self.assertEqual(
            self.caretaker.get_memento(0)._objective,
            "Find x",
        )
        self.assertEqual(
            self.caretaker.get_memento(1)._objective,
            "Minimize x",
        )

    def test_cannot_restore_another_problem(self):
        state = self.problem.create_memento()

        another_problem = MathematicalProblem()
        another_problem.problem_id = "problem-2"
        another_problem.original_prompt = "Solve y = 4"
        another_problem.objective = "Find y"
        another_problem.constraints = []

        with self.assertRaises(ValueError):
            another_problem.restore(state)

    def test_missing_saved_state_raises_error(self):
        with self.assertRaises(IndexError):
            self.caretaker.get_memento(0)


if __name__ == "__main__":
    unittest.main()
