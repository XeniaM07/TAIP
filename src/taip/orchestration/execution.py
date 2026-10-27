
from __future__ import annotations

from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ModelResponse,
    ModelStatus,
    VerificationResult,
)
from taip.mathematical_verification import VerificationHandler
from taip.orchestration.states import ExecutionState, ExecutionStateContext


class ModelExecution:
    """Represents one model execution within a shared execution run."""

    execution_run: ExecutionRunContract
    model_id: str
    status: ModelStatus
    response: ModelResponse | None


class ExecutionRunContext(ExecutionStateContext):
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

    def transition_to(self, state: ExecutionState) -> None:
        ...


class ExecutionResultCollector:
    """Collects every model outcome, including failed executions."""

    def collect(
        self,
        execution_run: ExecutionRunContext,
    ) -> tuple[ModelExecution, ModelExecution, ModelExecution]:
        ...


class VerificationCoordinator:
    """Coordinates mathematical verification of model responses."""

    verification_chain: VerificationHandler

    def verify(
        self,
        execution_run: ExecutionRunContext,
        responses: list[ModelResponse],
    ) -> list[VerificationResult]:
        ...
