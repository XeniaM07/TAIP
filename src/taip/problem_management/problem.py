
"""Mathematical problem domain model."""

from __future__ import annotations

from taip.common.contracts import Constraint
from taip.problem_management.memento import ProblemMemento


class MathematicalProblem:
    """Receiver for commands and originator for mementos."""

    problem_id: str
    original_prompt: str
    objective: str
    constraints: list[Constraint]

    def add_constraint(self, constraint: Constraint) -> None:
        ...

    def update_constraint(self, constraint: Constraint) -> None:
        ...

    def remove_constraint(self, constraint_id: str) -> None:
        ...

    def create_memento(self) -> ProblemMemento:
        ...

    def restore(self, memento: ProblemMemento) -> None:
        ...
