"""Application-facing AI execution outcomes without provider dependencies."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol
import math


@dataclass(frozen=True, slots=True)
class AICallFailure:
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AICallOutcome:
    result: object | None = field(default=None, repr=False)
    failure: AICallFailure | None = None
    duration_ms: float = 0
    invocation: Mapping[str, object] | None = None
    raw_response: str | None = field(default=None, repr=False, metadata={"serialize": False})
    exception: Exception | None = field(default=None, repr=False, compare=False, metadata={"serialize": False})
    attempted: bool = field(default=True, metadata={"serialize": False})


@dataclass(frozen=True, slots=True)
class AIAttempt:
    attempt_id: str
    operation: str
    model_id: str
    snapshot_id: str | None
    duration_ms: float
    failure: AICallFailure | None = None
    invocation: Mapping[str, object] | None = None
    raw_response: str | None = field(default=None, repr=False, metadata={"serialize": False})


class AIExecutionPort(Protocol):
    def execute(
        self, operation: Literal["extract", "solve"], calls: Mapping[str, Callable[[], object]],
    ) -> Mapping[str, AICallOutcome]: ...


_MESSAGES = {
    "timeout": "The AI provider timed out. Retry explicitly if needed.",
    "network_error": "The AI provider could not be reached.",
    "rate_limit": "The AI provider is rate limited. Wait before retrying.",
    "local_rate_limit": "The configured AI request budget is currently exhausted.",
    "admission_unavailable": "AI quota storage is unavailable.",
    "provider_unavailable": "The AI provider is temporarily unavailable.",
    "coordinator_busy": "AI execution is busy. Try again after the active operation finishes.",
    "auth_error": "The AI provider credentials were rejected.",
    "permission_denied": "The configured AI model is not available to this account.",
    "model_unavailable": "The configured AI model is unavailable.",
    "quota_exceeded": "The AI provider account quota is exhausted.",
    "request_budget_exceeded": "The request exceeds the configured AI token budget.",
    "invalid_json": "The AI response is not valid JSON.",
    "invalid_schema": "The AI response does not match the required schema.",
    "invalid_envelope": "The AI provider returned an invalid response envelope.",
    "incomplete_response": "The AI provider returned an incomplete response.",
    "output_truncated": "The AI response reached its output limit and is incomplete.",
    "provider_refusal": "The AI provider declined this request.",
    "provider_error": "The AI provider reported an error.",
    "http_error": "The AI provider rejected the HTTP request.",
    "unsupported_encoding": "The AI provider returned an unsupported response encoding.",
    "response_too_large": "The AI response exceeds the supported size.",
    "input_too_large": "The request exceeds the supported input size.",
    "resource_limit": "The timetable exceeds the supported resource budget.",
    "model_mismatch": "The AI response belongs to a different model or snapshot.",
    "invalid_request": "The AI request is invalid.",
    "configuration_error": "The AI provider configuration is invalid.",
}
_RETRYABLE = {"timeout", "network_error", "rate_limit", "local_rate_limit",
              "admission_unavailable", "provider_unavailable", "coordinator_busy"}


def classify_ai_failure(error: Exception) -> AICallFailure:
    """Project known categories only; arbitrary exception text stays private."""
    code = getattr(error, "code", None)
    if type(code) is not str or code not in _MESSAGES:
        return AICallFailure("unexpected_error", "The AI operation could not be completed.")
    delay = getattr(error, "retry_after_seconds", None)
    try:
        if type(delay) not in (int, float) or delay < 0 or not math.isfinite(delay):
            delay = None
    except OverflowError:
        delay = None
    return AICallFailure(code, _MESSAGES[code], code in _RETRYABLE, delay)
