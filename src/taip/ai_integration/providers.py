"""Timetable adapters for the three configured, non-streaming model services.

HTTP mechanics are injected. These adapters only build provider requests,
recognize complete final text, and decode it through the common JSON boundary.
They do not confirm domain facts or determine timetable feasibility.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import quote

from taip.ai_integration.config import ProviderConfig, ProviderKind, provider_quota_key
from taip.ai_integration.errors import (
    AIConfigurationError, AIProviderError, AIResponseError, provider_error_for_status,
)
from taip.ai_integration.http_transport import JSONTransport
from taip.ai_integration.monitoring import (
    InvocationObserver, capture_provider_response, monitor_ai_call,
)
from taip.ai_integration.prompts import (
    PromptMessages, build_extraction_prompt, build_solution_prompt,
)
from taip.ai_integration.rate_limits import RateLimiter, RateLimitPort
from taip.ai_integration.request_guard import (
    measure_request, validate_domain_input, validate_lesson_budget, validate_source_prompt,
)
from taip.ai_integration.response_codec import parse_extraction_response, parse_solution_response
from taip.ai_integration.response_schema import (
    extraction_json_schema, groq_response_schema, solution_json_schema,
)
from taip.ai_integration.telemetry import GenerationParameters, parse_provider_usage
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


def _gemini_text(candidate: dict[str, object]) -> str | None:
    content = candidate.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        return None
    text_parts = []
    for part in content["parts"]:
        if not isinstance(part, dict) or type(part.get("thought", False)) is not bool:
            return None
        if part.get("thought", False):
            continue
        if not isinstance(part.get("text"), str):
            return None
        text_parts.append(part["text"])
    return "".join(text_parts)


class TimetableAIAdapter(ABC):
    """Share canonical operations while concrete subclasses adapt REST formats."""

    __slots__ = ("_config", "_transport", "_observer", "_limiter", "_quota_key")
    _provider_kind: ProviderKind

    def __init__(
        self, config: ProviderConfig, transport: JSONTransport, *,
        observer: InvocationObserver | None = None,
        limiter: RateLimitPort | None = None,
    ) -> None:
        if not isinstance(config, ProviderConfig):
            raise AIConfigurationError("config")
        if config.provider is not self._provider_kind:
            raise AIConfigurationError("provider")
        if observer is not None and not callable(observer):
            raise AIConfigurationError("observer")
        self._config = config
        self._transport = transport
        self._observer = observer
        self._limiter = limiter if limiter is not None else RateLimiter()
        self._quota_key = provider_quota_key(config)

    @property
    def model_id(self) -> str:
        return self._config.model_id

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def provider_name(self) -> str:
        return self._config.provider.value

    @property
    def requested_model_id(self) -> str:
        return self._config.api_model_id

    @property
    def invocation_observer(self) -> InvocationObserver | None:
        return self._observer

    @monitor_ai_call("extract")
    def extract(self, problem: OriginalProblem) -> ExtractionProposal:
        self._capture_generation_parameters()
        validate_source_prompt(problem.prompt, max_bytes=self._config.max_prompt_bytes)
        completion = self._request(build_extraction_prompt(problem), extraction_json_schema())
        capture_provider_response(
            raw_response=completion.text, returned_model_id=completion.returned_model_id,
            upstream_provider=completion.upstream_provider,
        )
        return parse_extraction_response(completion.text, problem=problem, model_id=self.model_id)

    @monitor_ai_call("solve")
    def solve(self, snapshot: TimetableProblemSnapshot) -> TimetableSolution:
        self._capture_generation_parameters()
        if snapshot.model_id != self.model_id:
            raise AIResponseError("model_mismatch", "$.model_id", "Snapshot belongs to another model.")
        validate_lesson_budget(snapshot.data)
        validate_domain_input(snapshot.data, max_bytes=self._config.max_input_bytes)
        completion = self._request(build_solution_prompt(snapshot), solution_json_schema())
        capture_provider_response(
            raw_response=completion.text, returned_model_id=completion.returned_model_id,
            upstream_provider=completion.upstream_provider,
        )
        return parse_solution_response(completion.text, snapshot=snapshot, model_id=self.model_id)

    def _capture_generation_parameters(self) -> None:
        capture_provider_response(generation_parameters=GenerationParameters(
            temperature=self._config.temperature,
            max_output_tokens=self._config.max_output_tokens,
            use_json_schema=self._config.use_json_schema,
        ))

    def _post(self, url: str, headers: dict[str, str], body: dict[str, object]) -> dict[str, object]:
        size = measure_request(body, max_bytes=self._config.max_request_bytes)
        permit = self._limiter.acquire(
            self._quota_key, policy=self._config.rate_limits,
            estimated_tokens=size + self._config.max_output_tokens + 128,
            provider=self.provider_name, model_id=self.model_id,
        )
        usage = None
        cooldown = None
        try:
            response = self._transport.post(
                url=url, headers=headers, body=body,
                timeout_seconds=self._config.timeout_seconds,
                max_response_bytes=self._config.max_response_bytes,
            )
            envelope = _object(response)
            usage = parse_provider_usage(self._config.provider, envelope)
            model = envelope.get("modelVersion" if self._provider_kind is ProviderKind.GEMINI else "model")
            upstream = envelope.get("provider")
            capture_provider_response(
                returned_model_id=model if isinstance(model, str) and model.strip() else None,
                upstream_provider=upstream if isinstance(upstream, str) and upstream.strip() else None,
                usage=usage,
            )
            if "error" in envelope:
                _raise_envelope_error(envelope["error"])
            return envelope
        except AIProviderError as error:
            if error.code == "rate_limit":
                cooldown = 60.0 if error.retry_after_seconds is None else error.retry_after_seconds
            raise
        finally:
            permit.finish(actual_tokens=usage.total_tokens if usage else None, cooldown_seconds=cooldown)

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
        text = _gemini_text(candidate)
        capture_provider_response(raw_response=text)
        reason = candidate.get("finishReason")
        if reason in ("MAX_TOKENS", "CONTINUATION"):
            raise AIProviderError("output_truncated")
        if reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"):
            raise AIProviderError("provider_refusal")
        if reason != "STOP":
            raise AIProviderError("invalid_envelope")
        if text is None or not text.strip():
            raise AIProviderError("invalid_envelope")
        return ProviderCompletion(text, _optional_string(envelope.get("modelVersion")))


class _ChatTimetableAdapter(TimetableAIAdapter):
    __slots__ = ()
    _endpoint: str

    def _request(self, messages: PromptMessages, schema: dict[str, object]) -> ProviderCompletion:
        output_format: dict[str, object] = {"type": "json_object"}
        if self._config.use_json_schema:
            provider_schema = groq_response_schema(schema) if self._provider_kind is ProviderKind.GROQ else schema
            output_format = {"type": "json_schema", "json_schema": {
                "name": "timetable_response", "strict": True, "schema": provider_schema,
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
        partial_message = choice.get("message")
        if isinstance(partial_message, dict) and isinstance(partial_message.get("content"), str):
            capture_provider_response(raw_response=partial_message["content"])
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
