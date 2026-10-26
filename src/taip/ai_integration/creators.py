"""AI-02: declare the Factory Method hierarchy for the existing adapters."""

from abc import ABC, abstractmethod

from taip.ai_integration.adapters import (
    AIModelAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    ThirdModelAdapter,
)
from taip.ai_integration.provider_clients import (
    GeminiClient,
    OpenAIClient,
    ThirdModelClient,
)


class ModelAdapterCreator(ABC):
    """Abstract creator consumed without knowledge of concrete products."""

    @abstractmethod
    def create_adapter(self) -> AIModelAdapter:
        """Declare the factory method returning the common product interface."""
        ...


class OpenAIAdapterCreator(ModelAdapterCreator):
    """Declare creation of the OpenAI adapter using its client dependency."""

    model_id: str
    client: OpenAIClient

    def create_adapter(self) -> OpenAIAdapter:
        ...


class GeminiAdapterCreator(ModelAdapterCreator):
    """Declare creation of the Gemini adapter using its client dependency."""

    model_id: str
    client: GeminiClient

    def create_adapter(self) -> GeminiAdapter:
        ...


class ThirdModelAdapterCreator(ModelAdapterCreator):
    """Declare creation of an adapter without selecting a third provider."""

    model_id: str
    client: ThirdModelClient

    def create_adapter(self) -> ThirdModelAdapter:
        ...
