"""Offline timetable provider with caller-supplied, exact-input response scripts."""

from collections.abc import Mapping

from taip.ai_integration.errors import AIResponseError
from taip.ai_integration.monitoring import (
    InvocationObserver, capture_provider_response, monitor_ai_call,
)
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

    __slots__ = ("_model_id", "_extraction_responses", "_solution_responses", "_observer")

    def __init__(
        self,
        model_id: str,
        *,
        extraction_responses: Mapping[str, str],
        solution_responses: Mapping[str, str],
        observer: InvocationObserver | None = None,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("A non-empty model identifier is required.")
        if observer is not None and not callable(observer):
            raise ValueError("Invocation observer must be callable.")
        self._model_id = model_id
        self._extraction_responses = _copy_responses(extraction_responses)
        self._solution_responses = _copy_responses(solution_responses)
        self._observer = observer

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def requested_model_id(self) -> str:
        return self._model_id

    @property
    def invocation_observer(self) -> InvocationObserver | None:
        return self._observer

    @monitor_ai_call("extract")
    def extract(self, problem: OriginalProblem) -> ExtractionProposal:
        try:
            raw_response = self._extraction_responses[problem.prompt]
        except KeyError:
            raise ValueError("No extraction response is configured for this prompt.") from None
        capture_provider_response(raw_response=raw_response, returned_model_id=self.model_id)
        return parse_extraction_response(
            raw_response, problem=problem, model_id=self.model_id
        )

    @monitor_ai_call("solve")
    def solve(self, snapshot: TimetableProblemSnapshot) -> TimetableSolution:
        if snapshot.model_id != self.model_id:
            raise AIResponseError("model_mismatch", "$.model_id", "Snapshot belongs to another model.")
        try:
            raw_response = self._solution_responses[snapshot.snapshot_id]
        except KeyError:
            raise ValueError("No solution response is configured for this snapshot.") from None
        capture_provider_response(raw_response=raw_response, returned_model_id=self.model_id)
        return parse_solution_response(
            raw_response, snapshot=snapshot, model_id=self.model_id
        )
