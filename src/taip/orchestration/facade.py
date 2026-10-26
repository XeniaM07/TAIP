
from __future__ import annotations

from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
)
from taip.orchestration.execution import (
    ExecutionResultCollector,
    ExecutionRunContext,
    VerificationCoordinator,
)


class ModelExecutionCoordinator:
    """Coordinates the three model executions for one shared run."""

    def execute(
        self,
        execution_run: ExecutionRunContract,
    ) -> tuple[ModelResponse, ModelResponse, ModelResponse]:
        ...


class ProblemOrchestrator:
    """Orchestrates execution against one confirmed problem snapshot."""

    model_execution_coordinator: ModelExecutionCoordinator

    def execute(
        self,
        confirmed_snapshot: ProblemSnapshot,
    ) -> ExecutionRunContext:
        ...


class ExecutionFacade:
    """Entry point for execution, results, and verification."""

    problem_orchestrator: ProblemOrchestrator
    result_collector: ExecutionResultCollector
    verification_coordinator: VerificationCoordinator

    def start_execution(
        self,
        confirmed_snapshot: ProblemSnapshot,
    ) -> ExecutionRunContext:
        ...

    def get_results(
        self,
        execution_run: ExecutionRunContext,
    ) -> list[ModelResponse]:
        ...

    def get_verification_results(
        self,
        execution_run: ExecutionRunContext,
    ) -> list[VerificationResult]:
        ...
