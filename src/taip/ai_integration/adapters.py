"""AI-01: declare a common target and three provider object adapters."""

from abc import ABC, abstractmethod

from taip.ai_integration.provider_clients import (
    GeminiClient,
    OpenAIClient,
    ThirdModelClient,
)
from taip.common.contracts import ModelRequest, ModelResponse, ModelStatus

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

    
    def generate_solution(
        self, request: ModelRequest, *, run_id: str
    ) -> ModelResponse:
        if request.model_id != self.model_id:
            raise ValueError("Model identifier mismatch.")

        snapshot = request.snapshot

        prompt = (
            f"Problem: {snapshot.original_prompt}\n"
            f"Objective: {snapshot.objective}\n"
            "Constraints:\n"
            + "\n".join(
                constraint.expression
                for constraint in snapshot.constraints
            )
        )

        result = self.client.responses.create(
            model=self.model_id,
            input=prompt,
        )

        return ModelResponse(
            model_id=self.model_id,
            run_id=run_id,
            output=result.output_text,
            status=ModelStatus.COMPLETED,
        )

    def get_model_identifier(self) -> str:
        return self.model_id

    


class GeminiAdapter(AIModelAdapter):
    """Declare adaptation through ``client.models.generate_content``."""

    model_id: str
    client: GeminiClient

   
    def generate_solution(
        self, request: ModelRequest, *, run_id: str
    ) -> ModelResponse:
        if request.model_id != self.model_id:
            raise ValueError("Model identifier mismatch.")

        snapshot = request.snapshot

        prompt = (
            f"Problem: {snapshot.original_prompt}\n"
            f"Objective: {snapshot.objective}\n"
            "Constraints:\n"
            + "\n".join(
                constraint.expression
                for constraint in snapshot.constraints
            )
        )

        result = self.client.models.generate_content(
            model=self.model_id,
            contents=prompt,
        )

        return ModelResponse(
            model_id=self.model_id,
            run_id=run_id,
            output=result.text,
            status=ModelStatus.COMPLETED,
        )

    def get_model_identifier(self) -> str:
        return self.model_id

    

class ThirdModelAdapter(AIModelAdapter):
    """Declare adaptation through the proposed third-model client port."""

    model_id: str
    client: ThirdModelClient

    
    def generate_solution(
        self, request: ModelRequest, *, run_id: str
    ) -> ModelResponse:
        if request.model_id != self.model_id:
            raise ValueError("Model identifier mismatch.")

        snapshot = request.snapshot

        prompt = (
            f"Problem: {snapshot.original_prompt}\n"
            f"Objective: {snapshot.objective}\n"
            "Constraints:\n"
            + "\n".join(
                constraint.expression
                for constraint in snapshot.constraints
            )
        )

        output = self.client.generate(
            model=self.model_id,
            prompt=prompt,
        )

        return ModelResponse(
            model_id=self.model_id,
            run_id=run_id,
            output=output,
            status=ModelStatus.COMPLETED,
        )

    def get_model_identifier(self) -> str:
        return self.model_id


