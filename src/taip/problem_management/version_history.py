"""Declarations for preserving confirmed problem snapshot versions."""

from taip.common.contracts import ProblemSnapshot


class ProblemVersionHistory:
    """Stores and retrieves immutable confirmed snapshots by problem version."""

    snapshots: list[ProblemSnapshot]

    def add_snapshot(self, snapshot: ProblemSnapshot) -> None:
        ...

    def get_snapshot(self, problem_id: str, version: int) -> ProblemSnapshot:
        ...
