"""AI-01: declare a common target and three provider object adapters."""

from abc import ABC, abstractmethod

from taip.ai_integration.provider_clients import (
    GeminiClient,
    OpenAIClient,
    ThirdModelClient,
)
from taip.common.contracts import ModelRequest, ModelResponse


class AIModelAdapter(ABC):
    """Common target consumed by orchestration for one configured model.

    The caller supplies the application run ID and an approved snapshot inside
    ``ModelRequest``. Confirmation and mathematical verification are separate
    responsibilities; this interface declares no authorization or solving logic.
    """

    @abstractmethod
    def generate_solution(self, request: ModelRequest, *, run_id: str) -> ModelResponse:
        """Declare generation with caller-owned run correlation."""
        ...

    @abstractmethod
    def get_model_identifier(self) -> str:
        """Declare access to the configured application model identifier."""
        ...


class OpenAIAdapter(AIModelAdapter):
    """Declare adaptation through ``client.responses.create`` and output text."""

    model_id: str
    client: OpenAIClient

    def generate_solution(self, request: ModelRequest, *, run_id: str) -> ModelResponse:
        """Declare request translation and shared-response normalization."""
        ...

    def get_model_identifier(self) -> str:
        ...


class GeminiAdapter(AIModelAdapter):
    """Declare adaptation through ``client.models.generate_content``."""

    model_id: str
    client: GeminiClient

    def generate_solution(self, request: ModelRequest, *, run_id: str) -> ModelResponse:
        """Declare request translation and optional-text normalization."""
        ...

    def get_model_identifier(self) -> str:
        ...


class ThirdModelAdapter(AIModelAdapter):
    """Declare adaptation through the proposed third-model client port."""

    model_id: str
    client: ThirdModelClient

    def generate_solution(self, request: ModelRequest, *, run_id: str) -> ModelResponse:
        """Declare adaptation without choosing a concrete third provider."""
        ...

    def get_model_identifier(self) -> str:
        ...
