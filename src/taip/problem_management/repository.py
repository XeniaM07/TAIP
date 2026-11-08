"""Temporary in-memory storage for timetable drafts and snapshots."""

from taip.common.timetable_contracts import (
    ProblemDraft,
    TimetableProblemSnapshot,
)

from taip.problem_management.timetable_history import TimetableHistory
class InMemoryProblemRepository:
    """Stores timetable data for one active session."""

    def __init__(self) -> None:
        self.drafts: dict[str, ProblemDraft] = {}
        self.snapshots: dict[str, TimetableProblemSnapshot] = {}
        self.histories: dict[str, TimetableHistory] = {}

    def save_draft(self, draft: ProblemDraft) -> None:
        self.drafts[draft.draft_id] = draft

    def get_draft(self, draft_id: str) -> ProblemDraft:
        return self.drafts[draft_id]

    def save_snapshot(self, snapshot: TimetableProblemSnapshot) -> None:
        self.snapshots[snapshot.snapshot_id] = snapshot

    def get_snapshot(self, snapshot_id: str) -> TimetableProblemSnapshot:
        return self.snapshots[snapshot_id]
    def get_history(self, draft_id: str) -> TimetableHistory:
        """Get or create the history associated with a draft."""
        if draft_id not in self.drafts:
            raise KeyError(f"Draft '{draft_id}' not found.")

        if draft_id not in self.histories:
            self.histories[draft_id] = TimetableHistory()

        return self.histories[draft_id]