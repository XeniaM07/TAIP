"""Timetable adapters for the three configured, non-streaming model services.

HTTP mechanics are injected. These adapters only build provider requests,
recognize complete final text, and decode it through the common JSON boundary.
They do not confirm domain facts or determine timetable feasibility.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import quote

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import (
    AIConfigurationError, AIProviderError, AIResponseError, provider_error_for_status,
)
from taip.ai_integration.http_transport import JSONTransport
from taip.ai_integration.prompts import (
    PromptMessages, build_extraction_prompt, build_solution_prompt,
)
from taip.ai_integration.response_codec import parse_extraction_response, parse_solution_response
from taip.ai_integration.response_schema import extraction_json_schema, solution_json_schema
from taip.common.timetable_contracts import (
    ExtractionProposal, OriginalProblem, TimetableProblemSnapshot, TimetableSolution,
)


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """Final model text and the identity actually reported by its service."""

    text: str = field(repr=False)
    returned_model_id: str | None = None
    upstream_provider: str | None = None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AIProviderError("invalid_envelope")
    return value


def _single_result(value: object) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 1:
        raise AIProviderError("invalid_envelope")
    return _object(value[0])


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AIProviderError("invalid_envelope")
    return value


def _raise_envelope_error(error: object) -> None:
    if isinstance(error, dict):
        code = error.get("code")
        if type(code) is int and 400 <= code <= 599:
            raise provider_error_for_status(code)
    raise AIProviderError("provider_error")


class TimetableAIAdapter(ABC):
    """Share canonical operations while concrete subclasses adapt REST formats."""

    __slots__ = ("_config", "_transport")
    _provider_kind: ProviderKind

    def __init__(self, config: ProviderConfig, transport: JSONTransport) -> None:
        if not isinstance(config, ProviderConfig):
            raise AIConfigurationError("config")
        if config.provider is not self._provider_kind:
            raise AIConfigurationError("provider")
        self._config = config
        self._transport = transport

    @property
    def model_id(self) -> str:
        return self._config.model_id

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def extract(self, problem: OriginalProblem) -> ExtractionProposal:
        completion = self._request(build_extraction_prompt(problem), extraction_json_schema())
        return parse_extraction_response(completion.text, problem=problem, model_id=self.model_id)

    def solve(self, snapshot: TimetableProblemSnapshot) -> TimetableSolution:
        if snapshot.model_id != self.model_id:
            raise AIResponseError("model_mismatch", "$.model_id", "Snapshot belongs to another model.")
        completion = self._request(build_solution_prompt(snapshot), solution_json_schema())
        return parse_solution_response(completion.text, snapshot=snapshot, model_id=self.model_id)

    def _post(self, url: str, headers: dict[str, str], body: dict[str, object]) -> dict[str, object]:
        response = self._transport.post(
            url=url, headers=headers, body=body,
            timeout_seconds=self._config.timeout_seconds,
            max_response_bytes=self._config.max_response_bytes,
        )
        envelope = _object(response)
        if "error" in envelope:
            _raise_envelope_error(envelope["error"])
        return envelope

    @abstractmethod
    def _request(self, messages: PromptMessages, schema: dict[str, object]) -> ProviderCompletion:
        """Adapt one provider's request and final-response envelope."""


class GeminiTimetableAdapter(TimetableAIAdapter):
    __slots__ = ()
    _provider_kind = ProviderKind.GEMINI

    def _request(self, messages: PromptMessages, schema: dict[str, object]) -> ProviderCompletion:
        output_format: dict[str, object] = {"mimeType": "APPLICATION_JSON"}
        if self._config.use_json_schema:
            output_format["schema"] = schema
        body = {
            "systemInstruction": {"parts": [{"text": messages.system}]},
            "contents": [{"role": "user", "parts": [{"text": messages.user}]}],
            "generationConfig": {
                "temperature": self._config.temperature,
                "maxOutputTokens": self._config.max_output_tokens,
                "responseFormat": {"text": output_format},
            },
        }
        model_path = quote(self._config.api_model_id, safe="")
        envelope = self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}:generateContent",
            {"x-goog-api-key": self._config.api_key, "Content-Type": "application/json"}, body,
        )
        feedback = envelope.get("promptFeedback")
        if feedback is not None and _object(feedback).get("blockReason"):
            raise AIProviderError("provider_refusal")
        candidate = _single_result(envelope.get("candidates"))
        reason = candidate.get("finishReason")
        if reason in ("MAX_TOKENS", "CONTINUATION"):
            raise AIProviderError("output_truncated")
        if reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"):
            raise AIProviderError("provider_refusal")
        if reason != "STOP":
            raise AIProviderError("invalid_envelope")
        parts = _object(candidate.get("content")).get("parts")
        if not isinstance(parts, list):
            raise AIProviderError("invalid_envelope")
        text_parts = []
        for value in parts:
            part = _object(value)
            thought = part.get("thought", False)
            if type(thought) is not bool:
                raise AIProviderError("invalid_envelope")
            if thought:
                continue
            text = part.get("text")
            if not isinstance(text, str):
                raise AIProviderError("invalid_envelope")
            text_parts.append(text)
        text = "".join(text_parts)
        if not text.strip():
            raise AIProviderError("invalid_envelope")
        return ProviderCompletion(text, _optional_string(envelope.get("modelVersion")))


class _ChatTimetableAdapter(TimetableAIAdapter):
    __slots__ = ()
    _endpoint: str

    def _request(self, messages: PromptMessages, schema: dict[str, object]) -> ProviderCompletion:
        output_format: dict[str, object] = {"type": "json_object"}
        if self._config.use_json_schema:
            output_format = {"type": "json_schema", "json_schema": {
                "name": "timetable_response", "strict": True, "schema": schema,
            }}
        body: dict[str, object] = {
            "model": self._config.api_model_id,
            "messages": [
                {"role": "system", "content": messages.system},
                {"role": "user", "content": messages.user},
            ],
            "stream": False,
            "temperature": self._config.temperature,
            "max_completion_tokens": self._config.max_output_tokens,
            "response_format": output_format,
        }
        if self._provider_kind is ProviderKind.OPENROUTER:
            body["provider"] = {"allow_fallbacks": False, "require_parameters": True}
        envelope = self._post(self._endpoint, {
            "Authorization": f"Bearer {self._config.api_key}", "Content-Type": "application/json",
        }, body)
        choice = _single_result(envelope.get("choices"))
        if "error" in choice:
            _raise_envelope_error(choice["error"])
        message = _object(choice.get("message"))
        if message.get("refusal"):
            raise AIProviderError("provider_refusal")
        reason = choice.get("finish_reason")
        if reason == "length":
            raise AIProviderError("output_truncated")
        if reason == "content_filter":
            raise AIProviderError("provider_refusal")
        if reason == "error":
            raise AIProviderError("provider_error")
        if reason != "stop":
            raise AIProviderError("invalid_envelope")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise AIProviderError("invalid_envelope")
        if message.get("tool_calls") or message.get("function_call"):
            raise AIProviderError("invalid_envelope")
        return ProviderCompletion(
            text, _optional_string(envelope.get("model")),
            _optional_string(envelope.get("provider")),
        )


class GroqTimetableAdapter(_ChatTimetableAdapter):
    __slots__ = ()
    _provider_kind = ProviderKind.GROQ
    _endpoint = "https://api.groq.com/openai/v1/chat/completions"


class OpenRouterTimetableAdapter(_ChatTimetableAdapter):
    __slots__ = ()
    _provider_kind = ProviderKind.OPENROUTER
    _endpoint = "https://openrouter.ai/api/v1/chat/completions"
