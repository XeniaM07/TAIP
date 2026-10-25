
"""Mathematical problem domain model."""

from __future__ import annotations

from taip.common.contracts import Constraint


class MathematicalProblem:
    """Receiver for constraint editing commands."""

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
