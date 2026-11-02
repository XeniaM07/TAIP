
"""Factory Method implementation for AI model adapters."""

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
    """Abstract creator for AI model adapters."""

    @abstractmethod
    def create_adapter(self) -> AIModelAdapter:
        """Create an adapter through the common interface."""
        ...


class OpenAIAdapterCreator(ModelAdapterCreator):
    """Factory for OpenAI adapters."""

    model_id: str
    client: OpenAIClient

    def create_adapter(self) -> OpenAIAdapter:
        adapter = OpenAIAdapter()
        adapter.model_id = self.model_id
        adapter.client = self.client
        return adapter


class GeminiAdapterCreator(ModelAdapterCreator):
    """Factory for Gemini adapters."""

    model_id: str
    client: GeminiClient

    def create_adapter(self) -> GeminiAdapter:
        adapter = GeminiAdapter()
        adapter.model_id = self.model_id
        adapter.client = self.client
        return adapter


class ThirdModelAdapterCreator(ModelAdapterCreator):
    """Factory for the third AI model adapter."""

    model_id: str
    client: ThirdModelClient

    def create_adapter(self) -> ThirdModelAdapter:
        adapter = ThirdModelAdapter()
        adapter.model_id = self.model_id
        adapter.client = self.client
        return adapter
