
"""Store and retrieve confirmed mathematical problem snapshots."""

from taip.common.contracts import ProblemSnapshot


class ProblemVersionHistory:
    """Stores immutable snapshots indexed by problem and version."""

    snapshots: list[ProblemSnapshot]

    def __init__(self) -> None:
        self.snapshots = []

    def add_snapshot(self, snapshot: ProblemSnapshot) -> None:
        """Store a confirmed snapshot without overwriting versions."""
        if snapshot.version < 1:
            raise ValueError("Snapshot version must be positive.")

        if any(
            existing.problem_id == snapshot.problem_id
            and existing.version == snapshot.version
            for existing in self.snapshots
        ):
            raise ValueError(
                "A snapshot with this problem ID and version "
                "already exists."
            )

        self.snapshots.append(snapshot)

    def get_snapshot(
        self, problem_id: str, version: int
    ) -> ProblemSnapshot:
        """Retrieve a previously confirmed problem version."""
        for snapshot in self.snapshots:
            if (
                snapshot.problem_id == problem_id
                and snapshot.version == version
            ):
                return snapshot

        raise KeyError(
            f"Snapshot {problem_id}, version {version}, not found."
        )
