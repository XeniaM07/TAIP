
"""Invoker for constraint editing commands."""

from __future__ import annotations

from taip.problem_management.commands import ConstraintCommand


class ProblemEditor:
    """Invoker responsible for dispatching commands."""

    def execute_command(self, command: ConstraintCommand) -> None:
        ...
