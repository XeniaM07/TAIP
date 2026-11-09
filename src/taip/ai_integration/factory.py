"""Factory Method creators for the canonical timetable AI provider boundary."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import ClassVar

from taip.ai_integration.config import ProviderConfig, ProviderKind, provider_quota_key
from taip.ai_integration.errors import AIConfigurationError
from taip.ai_integration.http_transport import JSONTransport
from taip.ai_integration.monitoring import InvocationObserver
from taip.ai_integration.rate_limits import QuotaSnapshot, RateLimiter, RateLimitPort
from taip.ai_integration.providers import (
    GeminiTimetableAdapter,
    GroqTimetableAdapter,
    OpenRouterTimetableAdapter,
)
from taip.common.timetable_protocols import AIProviderPort


class TimetableAdapterCreator(ABC):
    """Concrete creators bind one configured provider to its matching adapter."""

    __slots__ = ("_config", "_transport", "_observer", "_limiter")
    _provider_kind: ClassVar[ProviderKind]

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
        self._limiter = limiter

    @abstractmethod
    def create_adapter(self) -> AIProviderPort:
        """Build the provider behind the canonical interface."""
        ...


class GeminiTimetableAdapterCreator(TimetableAdapterCreator):
    __slots__ = ()
    _provider_kind = ProviderKind.GEMINI

    def create_adapter(self) -> GeminiTimetableAdapter:
        return GeminiTimetableAdapter(self._config, self._transport, observer=self._observer, limiter=self._limiter)


class GroqTimetableAdapterCreator(TimetableAdapterCreator):
    __slots__ = ()
    _provider_kind = ProviderKind.GROQ

    def create_adapter(self) -> GroqTimetableAdapter:
        return GroqTimetableAdapter(self._config, self._transport, observer=self._observer, limiter=self._limiter)


class OpenRouterTimetableAdapterCreator(TimetableAdapterCreator):
    __slots__ = ()
    _provider_kind = ProviderKind.OPENROUTER

    def create_adapter(self) -> OpenRouterTimetableAdapter:
        return OpenRouterTimetableAdapter(self._config, self._transport, observer=self._observer, limiter=self._limiter)


_CREATOR_TYPES: Mapping[ProviderKind, type[TimetableAdapterCreator]] = MappingProxyType({
    ProviderKind.GEMINI: GeminiTimetableAdapterCreator,
    ProviderKind.GROQ: GroqTimetableAdapterCreator,
    ProviderKind.OPENROUTER: OpenRouterTimetableAdapterCreator,
})


class AIProviderFactory:
    """Expose eager, stable adapters while the caller owns the shared transport.

    Construction validates the full configuration before creating any adapter.
    Model lookups never substitute another model or perform a network request.
    Reuse the factory for the application's lifetime. For multiple factories,
    inject the same ``RateLimiter(observer=quota_callback)``; quota_status()
    exposes safe counters and the callback receives typed warning events.
    """

    __slots__ = ("_providers", "_model_ids", "_limiter", "_quota_keys")

    def __init__(
        self, configs: Iterable[ProviderConfig], *, transport: JSONTransport,
        observer: InvocationObserver | None = None,
        limiter: RateLimitPort | None = None,
    ) -> None:
        if observer is not None and not callable(observer):
            raise AIConfigurationError("observer")
        self._limiter = limiter if limiter is not None else RateLimiter()
        try:
            configuration_iterator = iter(configs)
        except TypeError:
            raise AIConfigurationError("providers") from None
        creators: dict[str, TimetableAdapterCreator] = {}
        policies = {}
        quota_keys = {}
        for config in configuration_iterator:
            if not isinstance(config, ProviderConfig):
                raise AIConfigurationError("config")
            if config.model_id in creators:
                raise AIConfigurationError("model_id")
            key = provider_quota_key(config)
            if key in policies and policies[key] != config.rate_limits:
                raise AIConfigurationError("rate_limits")
            policies[key] = config.rate_limits
            quota_keys[config.model_id] = key
            creators[config.model_id] = _CREATOR_TYPES[config.provider](
                config, transport, observer=observer, limiter=self._limiter,
            )
        if not creators:
            raise AIConfigurationError("providers")
        self._providers = MappingProxyType({
            model_id: creator.create_adapter() for model_id, creator in creators.items()
        })
        self._model_ids = tuple(self._providers)
        self._quota_keys = MappingProxyType(quota_keys)

    @property
    def MODEL_IDS(self) -> tuple[str, ...]:
        """Configured logical aliases in the caller's explicit order."""
        return self._model_ids

    def get_provider(self, model_id: str) -> AIProviderPort:
        if not isinstance(model_id, str) or model_id not in self._providers:
            raise KeyError("The requested AI model is not configured.")
        return self._providers[model_id]

    def quota_status(self, model_id: str) -> QuotaSnapshot:
        """Expose safe counters for UI/API integration, never credential keys.

        Reuse one factory per application, or inject a shared RateLimitPort into
        multiple factories. Distributed deployments need a shared-store gate.
        """
        self.get_provider(model_id)
        return self._limiter.snapshot(self._quota_keys[model_id])
