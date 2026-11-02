
"""Caretaker for saved mathematical problem mementos."""

from __future__ import annotations

from taip.problem_management.memento import ProblemMemento


class ProblemCaretaker:
    """Stores references to captured draft states."""

    mementos: list[ProblemMemento]

    def __init__(self) -> None:
        self.mementos = []

    def save(self, memento: ProblemMemento) -> None:
        """Store a captured problem state."""
        self.mementos.append(memento)

    def get_memento(self, index: int) -> ProblemMemento:
        """Return a previously saved state by index."""
        if index < 0 or index >= len(self.mementos):
            raise IndexError("Memento index is out of range.")

        return self.mementos[index]
