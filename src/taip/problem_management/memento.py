
"""Memento pattern for mathematical problem draft states."""

from __future__ import annotations

from dataclasses import dataclass

from taip.common.contracts import Constraint


@dataclass(frozen=True, slots=True)
class ProblemMemento:
    """Immutable representation of a captured draft state."""

    _problem_id: str
    _objective: str
    _constraints: tuple[Constraint, ...]
