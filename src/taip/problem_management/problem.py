
"""Mathematical problem domain model."""

from __future__ import annotations

from taip.common.contracts import Constraint, ProblemSnapshot
from taip.problem_management.memento import ProblemMemento
from taip.problem_management.version_history import ProblemVersionHistory


class MathematicalProblem:
    """Receiver for commands and originator for mementos."""

    problem_id: str
    original_prompt: str
    objective: str
    constraints: list[Constraint]
    version_history: ProblemVersionHistory

    def add_constraint(self, constraint: Constraint) -> None:
        ...

    def update_constraint(self, constraint: Constraint) -> None:
        ...

    def remove_constraint(self, constraint_id: str) -> None:
        ...

    def confirm(self, version: int) -> ProblemSnapshot:
        ...

    def create_memento(self) -> ProblemMemento:
        ...

    def restore(self, memento: ProblemMemento) -> None:
        ...
