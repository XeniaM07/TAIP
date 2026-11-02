
"""Execution context, result collection and verification coordination."""

from __future__ import annotations

from dataclasses import replace

from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ExecutionStatus,
    ModelResponse,
    ModelStatus,
    VerificationResult,
)
from taip.mathematical_verification import VerificationHandler
from taip.orchestration.states import (
    ExecutionState,
    ExecutionStateContext,
    PendingState,
)


class ModelExecution:
    """Represents one model outcome within a shared execution run."""

    execution_run: ExecutionRunContract
    model_id: str
    status: ModelStatus
    response: ModelResponse | None

    def __init__(
        self,
        execution_run: ExecutionRunContract,
        model_id: str,
        status: ModelStatus,
        response: ModelResponse | None,
    ) -> None:
        self.execution_run = execution_run
        self.model_id = model_id
        self.status = status
        self.response = response


class ExecutionRunContext(ExecutionStateContext):
    """Context managing execution state and model outcomes."""

    run: ExecutionRunContract
    current_state: ExecutionState
    model_executions: tuple[
        ModelExecution,
        ModelExecution,
        ModelExecution,
    ]

    def __init__(self, run: ExecutionRunContract) -> None:
        if run.status != ExecutionStatus.PENDING:
            raise ValueError("A new execution must start as PENDING.")

        self.run = run
        self.current_state = PendingState()
        self.model_executions = ()

    def start(self) -> None:
        self.current_state.start(self)

    def complete(self) -> None:
        self.current_state.complete(self)

    def fail(self) -> None:
        self.current_state.fail(self)

    def transition_to(self, state: ExecutionState) -> None:
        """Synchronize state object and immutable run contract."""
        self.current_state = state
        self.run = replace(
            self.run,
            status=state.status,
        )

    def record_responses(
        self,
        responses: tuple[
            ModelResponse,
            ModelResponse,
            ModelResponse,
        ],
    ) -> None:
        """Store outcomes and calculate the final execution status."""
        if self.run.status != ExecutionStatus.RUNNING:
            raise ValueError(
                "Responses can be recorded only while running."
            )

        if len(responses) != 3:
            raise ValueError("Exactly three responses are required.")

        for expected_id, response in zip(
            self.run.model_ids, responses
        ):
            if response.model_id != expected_id:
                raise ValueError("Response model identifier mismatch.")

            if response.run_id != self.run.run_id:
                raise ValueError("Response run identifier mismatch.")

        outcomes = tuple(
            ModelExecution(
                execution_run=self.run,
                model_id=response.model_id,
                status=response.status,
                response=response,
            )
            for response in responses
        )

        successful = sum(
            response.status == ModelStatus.COMPLETED
            for response in responses
        )

        if successful == 3:
            self.complete()
        elif successful == 0:
            self.fail()
        else:
            from taip.orchestration.states import PartialState
            self.transition_to(PartialState())

        # Synchronize the final run contract in all recorded outcomes.
        self.model_executions = tuple(
            ModelExecution(
                execution_run=self.run,
                model_id=item.model_id,
                status=item.status,
                response=item.response,
            )
            for item in outcomes
        )


class ExecutionResultCollector:
    """Collect all three model outcomes, including failures."""

    def collect(
        self,
        execution_run: ExecutionRunContext,
    ) -> tuple[ModelExecution, ModelExecution, ModelExecution]:
        if len(execution_run.model_executions) != 3:
            raise ValueError(
                "Execution does not yet contain three outcomes."
            )

        return execution_run.model_executions


class VerificationCoordinator:
    """Apply the verification chain to completed model responses."""

    verification_chain: VerificationHandler

    def verify(
        self,
        execution_run: ExecutionRunContext,
        responses: list[ModelResponse],
    ) -> list[VerificationResult]:
        results: list[VerificationResult] = []

        for response in responses:
            if response.status != ModelStatus.COMPLETED:
                continue

            if response.run_id != execution_run.run.run_id:
                raise ValueError("Response belongs to another run.")

            if response.model_id not in execution_run.run.model_ids:
                raise ValueError("Unexpected model identifier.")

            result = self.verification_chain.handle(
                execution_run.run.snapshot,
                response,
            )
            results.append(result)

        return results