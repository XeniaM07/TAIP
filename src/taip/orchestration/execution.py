
from __future__ import annotations

from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
)
from taip.orchestration.states import ExecutionState


class ModelExecution:
    """Represents one model execution within a shared execution run."""

    execution_run: ExecutionRunContract
    model_id: str
    status: ModelStatus
    response: ModelResponse | None


class ExecutionRunContext:
    """State pattern context for a run using the shared run contract."""

    run: ExecutionRunContract
    current_state: ExecutionState
    model_executions: tuple[
        ModelExecution,
        ModelExecution,
        ModelExecution,
    ]

    def start(self) -> None:
        ...

    def complete(self) -> None:
        ...

    def fail(self) -> None:
        ...


class ExecutionResultCollector:
    """Collects responses from the model executions in a run."""

    def collect(
        self,
        execution_run: ExecutionRunContext,
    ) -> list[ModelResponse]:
        ...


class VerificationCoordinator:
    """Coordinates mathematical verification of model responses."""

    def verify(
        self,
        execution_run: ExecutionRunContext,
        responses: list[ModelResponse],
    ) -> list[VerificationResult]:
        ...
