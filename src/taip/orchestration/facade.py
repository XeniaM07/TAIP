
from __future__ import annotations

from taip.ai_integration.adapters import AIModelAdapter
from taip.ai_integration.creators import ModelAdapterCreator
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
    """Declare execution through canonical AI creator and adapter abstractions.

    The configured creators correspond to the run's three model identifiers.
    These relationships do not initialize dependencies or validate identities.
    """

    adapter_creators: tuple[
        ModelAdapterCreator,
        ModelAdapterCreator,
        ModelAdapterCreator,
    ]

    def create_adapters(
        self,
    ) -> tuple[AIModelAdapter, AIModelAdapter, AIModelAdapter]:
        """Declare three adapter products supplied by the abstract creators."""
        ...

    def execute(
        self,
        execution_run: ExecutionRunContract,
    ) -> tuple[ModelResponse, ModelResponse, ModelResponse]:
        """Declare shared responses without implementing dispatch.

        Future ModelRequest values reuse execution_run.snapshot and the matching
        model identifier. Each adapter receives run_id=execution_run.run_id as
        a keyword argument; correlation enforcement remains future behavior.
        """
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
