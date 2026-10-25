
"""Command pattern for mathematical constraint editing."""

from __future__ import annotations

from abc import ABC, abstractmethod

from taip.common.contracts import Constraint
from taip.problem_management.problem import MathematicalProblem


class ConstraintCommand(ABC):
    """Abstract command interface."""

    @abstractmethod
    def execute(self) -> None:
        ...


class AddConstraintCommand(ConstraintCommand):
    """Command for adding a constraint."""

    problem: MathematicalProblem
    constraint: Constraint

    def execute(self) -> None:
        ...


class UpdateConstraintCommand(ConstraintCommand):
    """Command for updating an existing constraint."""

    problem: MathematicalProblem
    constraint: Constraint

    def execute(self) -> None:
        ...


class RemoveConstraintCommand(ConstraintCommand):
    """Command for removing a constraint."""

    problem: MathematicalProblem
    constraint_id: str

    def execute(self) -> None:
        ...
