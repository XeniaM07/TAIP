"""Memento pattern for timetable draft history."""

from dataclasses import dataclass, field

from taip.common.timetable_contracts import ProblemDraft


@dataclass(frozen=True, slots=True)
class TimetableMemento:
    """Immutable saved state of a timetable draft."""

    draft: ProblemDraft


@dataclass
class TimetableHistory:
    """Caretaker managing undo and redo states."""

    undo_stack: list[TimetableMemento] = field(default_factory=list)
    redo_stack: list[TimetableMemento] = field(default_factory=list)

    def record(self, draft: ProblemDraft) -> None:
        """Save the state before an edit."""
        self.undo_stack.append(TimetableMemento(draft))
        self.redo_stack.clear()

    def undo(self, current: ProblemDraft) -> ProblemDraft:
        if not self.undo_stack:
            raise ValueError("Nothing to undo.")

        previous = self.undo_stack.pop()
        self.redo_stack.append(TimetableMemento(current))
        return previous.draft

    def redo(self, current: ProblemDraft) -> ProblemDraft:
        if not self.redo_stack:
            raise ValueError("Nothing to redo.")

        next_state = self.redo_stack.pop()
        self.undo_stack.append(TimetableMemento(current))
        return next_state.draft