
from __future__ import annotations

from taip.ai_integration import AIModelAdapter, ModelAdapterCreator
from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ModelRequest,
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
)
from taip.orchestration.execution import (
    ExecutionResultCollector,
    ExecutionRunContext,
    ModelExecution,
    VerificationCoordinator,
)


class ModelExecutionCoordinator:
    """Coordinates the three model executions for one shared run."""

    adapter_creators: tuple[
        ModelAdapterCreator,
        ModelAdapterCreator,
        ModelAdapterCreator,
    ]

    def create_adapters(
        self,
    ) -> tuple[AIModelAdapter, AIModelAdapter, AIModelAdapter]:
        ...

    def create_requests(
        self,
        execution_run: ExecutionRunContract,
    ) -> tuple[ModelRequest, ModelRequest, ModelRequest]:
        ...

    def execute(
        self,
        execution_run: ExecutionRunContract,
        model_adapters: tuple[
            AIModelAdapter,
            AIModelAdapter,
            AIModelAdapter,
        ],
    ) -> tuple[ModelResponse, ModelResponse, ModelResponse]:
        ...


class ProblemOrchestrator:
    """Orchestrates execution against a snapshot produced by confirmation."""

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
    ) -> tuple[ModelExecution, ModelExecution, ModelExecution]:
        ...

    def get_verification_results(
        self,
        execution_run: ExecutionRunContext,
    ) -> list[VerificationResult]:
        ...
