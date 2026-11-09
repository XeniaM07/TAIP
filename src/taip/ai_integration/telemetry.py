"""Immutable generation controls and optional, provider-reported usage."""

from collections.abc import Mapping
from dataclasses import dataclass
import math

from taip.ai_integration.config import ProviderKind


@dataclass(frozen=True, slots=True)
class GenerationParameters:
    """The effective generation controls, without credentials or prompt data."""

    temperature: float
    max_output_tokens: int
    use_json_schema: bool


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Reported usage only; an absent field is unknown rather than zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _reported_cost(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        cost = float(value)
    except OverflowError:
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def parse_provider_usage(
    provider: ProviderKind, envelope: Mapping[str, object]
) -> ProviderUsage | None:
    """Keep only valid usage fields explicitly reported by this provider.

    Missing or malformed optional metadata is ignored field by field. Counts
    remain independent: totals, thinking tokens, and costs are never inferred.
    Only OpenRouter's reported account charge is interpreted as a USD cost.
    """
    if provider is ProviderKind.GEMINI:
        reported = _mapping(envelope.get("usageMetadata"))
        usage = ProviderUsage(
            input_tokens=_token_count(reported.get("promptTokenCount")),
            output_tokens=_token_count(reported.get("candidatesTokenCount")),
            total_tokens=_token_count(reported.get("totalTokenCount")),
            reasoning_tokens=_token_count(reported.get("thoughtsTokenCount")),
            cached_input_tokens=_token_count(reported.get("cachedContentTokenCount")),
        )
    elif provider in (ProviderKind.GROQ, ProviderKind.OPENROUTER):
        reported = _mapping(envelope.get("usage"))
        completion_details = _mapping(reported.get("completion_tokens_details"))
        prompt_details = _mapping(reported.get("prompt_tokens_details"))
        usage = ProviderUsage(
            input_tokens=_token_count(reported.get("prompt_tokens")),
            output_tokens=_token_count(reported.get("completion_tokens")),
            total_tokens=_token_count(reported.get("total_tokens")),
            reasoning_tokens=_token_count(completion_details.get("reasoning_tokens")),
            cached_input_tokens=_token_count(prompt_details.get("cached_tokens")),
            cost_usd=(
                _reported_cost(reported.get("cost"))
                if provider is ProviderKind.OPENROUTER else None
            ),
        )
    else:
        return None
    return None if usage == ProviderUsage() else usage
