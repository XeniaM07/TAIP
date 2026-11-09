"""Offline timetable provider with caller-supplied, exact-input response scripts."""

from collections.abc import Mapping

from taip.ai_integration.response_codec import (
    parse_extraction_response,
    parse_solution_response,
)
from taip.common.timetable_contracts import (
    ExtractionProposal,
    OriginalProblem,
    TimetableProblemSnapshot,
    TimetableSolution,
)


def _copy_responses(responses: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(responses, Mapping):
        raise ValueError("Response scripts must be mappings of strings to strings.")
    copied = dict(responses)
    if any(
        not isinstance(key, str) or not isinstance(response, str)
        for key, response in copied.items()
    ):
        raise ValueError("Response scripts must be mappings of strings to strings.")
    return copied


class MockAIProvider:
    """Decode scripted AI responses without inventing extraction or solving output."""

    __slots__ = ("_model_id", "_extraction_responses", "_solution_responses")

    def __init__(
        self,
        model_id: str,
        *,
        extraction_responses: Mapping[str, str],
        solution_responses: Mapping[str, str],
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("A non-empty model identifier is required.")
        self._model_id = model_id
        self._extraction_responses = _copy_responses(extraction_responses)
        self._solution_responses = _copy_responses(solution_responses)

    @property
    def model_id(self) -> str:
        return self._model_id

    def extract(self, problem: OriginalProblem) -> ExtractionProposal:
        try:
            raw_response = self._extraction_responses[problem.prompt]
        except KeyError:
            raise ValueError("No extraction response is configured for this prompt.") from None
        return parse_extraction_response(
            raw_response, problem=problem, model_id=self.model_id
        )

    def solve(self, snapshot: TimetableProblemSnapshot) -> TimetableSolution:
        try:
            raw_response = self._solution_responses[snapshot.snapshot_id]
        except KeyError:
            raise ValueError("No solution response is configured for this snapshot.") from None
        return parse_solution_response(
            raw_response, snapshot=snapshot, model_id=self.model_id
        )
