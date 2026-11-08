"""Session-scoped in-memory storage for timetable benchmark runs."""

from taip.benchmark.models import TimetableRunRecord
from taip.common.timetable_contracts import ModelRunResult


class InMemoryBenchmarkRepository:
    """Keep transient benchmark data isolated by session and comparison."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str, str], TimetableRunRecord] = {}
        self._legacy_runs: dict[tuple[str, str, str], ModelRunResult] = {}

    def save_run(self, record: TimetableRunRecord) -> None:
        key = (record.session_id, record.comparison_id, record.model_id)
        self._runs[key] = record

    def get_run(
        self, session_id: str, comparison_id: str, model_id: str
    ) -> TimetableRunRecord:
        return self._runs[(session_id, comparison_id, model_id)]

    def list_runs(
        self,
        session_id: str,
        comparison_id: str,
        problem_id: str,
    ) -> tuple[TimetableRunRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for (stored_session, stored_comparison, _), record
                    in self._runs.items()
                    if stored_session == session_id
                    and stored_comparison == comparison_id
                    and record.problem_id == problem_id
                ),
                key=lambda record: record.model_id,
            )
        )

    def save_legacy_run(
        self, session_id: str, comparison_id: str, result: ModelRunResult
    ) -> None:
        key = (session_id, comparison_id, result.model_id)
        self._legacy_runs[key] = result

    def list_legacy_runs(
        self, session_id: str, comparison_id: str
    ) -> tuple[ModelRunResult, ...]:
        return tuple(
            sorted(
                (
                    result
                    for (stored_session, stored_comparison, _), result
                    in self._legacy_runs.items()
                    if stored_session == session_id
                    and stored_comparison == comparison_id
                ),
                key=lambda result: result.model_id,
            )
        )
