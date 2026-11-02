
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
        """Add a new constraint to the problem."""
        if any(
            existing.constraint_id == constraint.constraint_id
            for existing in self.constraints
        ):
            raise ValueError(
                f"Constraint '{constraint.constraint_id}' already exists."
            )

        self.constraints.append(constraint)

    def update_constraint(self, constraint: Constraint) -> None:
        """Replace an existing constraint by identifier."""
        for index, existing in enumerate(self.constraints):
            if existing.constraint_id == constraint.constraint_id:
                self.constraints[index] = constraint
                return

        raise ValueError(
            f"Constraint '{constraint.constraint_id}' not found."
        )

    def remove_constraint(self, constraint_id: str) -> None:
        """Remove an existing constraint by identifier."""
        for index, constraint in enumerate(self.constraints):
            if constraint.constraint_id == constraint_id:
                del self.constraints[index]
                return

        raise ValueError(
            f"Constraint '{constraint_id}' not found."
        )

    def confirm(self, version: int) -> ProblemSnapshot:
        ...

    def create_memento(self) -> ProblemMemento:
        ...

    def restore(self, memento: ProblemMemento) -> None:
        ...
