
"""Caretaker for saved mathematical problem mementos."""

from __future__ import annotations

from taip.problem_management.memento import ProblemMemento


class ProblemCaretaker:
    """Stores references to captured draft states."""

    mementos: list[ProblemMemento]

    def save(self, memento: ProblemMemento) -> None:
        ...

    def get_memento(self, index: int) -> ProblemMemento:
        ...
