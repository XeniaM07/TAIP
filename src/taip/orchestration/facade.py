
from __future__ import annotations
from uuid import uuid4

from taip.ai_integration import AIModelAdapter, ModelAdapterCreator
from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ModelRequest,
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
    ModelStatus,
    ExecutionStatus,
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
        """Create the three configured adapters using Factory Method."""
        if len(self.adapter_creators) != 3:
            raise ValueError("Exactly three adapter creators are required.")

        adapters = tuple(
            creator.create_adapter()
            for creator in self.adapter_creators
        )

        model_ids = tuple(
            adapter.get_model_identifier()
            for adapter in adapters
        )

        if len(set(model_ids)) != 3:
            raise ValueError("Adapter model identifiers must be unique.")

        return adapters

    def create_requests(
            self,
            execution_run: ExecutionRunContract,
    ) -> tuple[ModelRequest, ModelRequest, ModelRequest]:
        """Create one model request per model using the same snapshot."""
        if len(execution_run.model_ids) != 3:
            raise ValueError("Exactly three model identifiers are required.")

        if len(set(execution_run.model_ids)) != 3:
            raise ValueError("Model identifiers must be unique.")

        return tuple(
            ModelRequest(
                model_id=model_id,
                snapshot=execution_run.snapshot,
            )
            for model_id in execution_run.model_ids
        )

    def execute(
            self,
            execution_run: ExecutionRunContract,
            model_adapters: tuple[
                AIModelAdapter,
                AIModelAdapter,
                AIModelAdapter,
            ],
    ) -> tuple[ModelResponse, ModelResponse, ModelResponse]:
        """Execute all three adapters, isolating provider failures."""
        if len(model_adapters) != 3:
            raise ValueError("Exactly three model adapters are required.")

        if len(set(execution_run.model_ids)) != 3:
            raise ValueError("Model identifiers must be unique.")

        for expected_id, adapter in zip(
                execution_run.model_ids, model_adapters
        ):
            if adapter.get_model_identifier() != expected_id:
                raise ValueError(
                    f"Adapter does not match model {expected_id}."
                )

        requests = self.create_requests(execution_run)
        responses: list[ModelResponse] = []

        for adapter, request in zip(model_adapters, requests):
            try:
                response = adapter.generate_solution(
                    request,
                    run_id=execution_run.run_id,
                )

                if (
                        response.model_id != request.model_id
                        or response.run_id != execution_run.run_id
                ):
                    raise ValueError(
                        "Provider returned inconsistent identifiers."
                    )

                responses.append(response)

            except Exception:
                responses.append(
                    ModelResponse(
                        model_id=request.model_id,
                        run_id=execution_run.run_id,
                        output=None,
                        status=ModelStatus.FAILED,
                    )
                )

        return tuple(responses)


class ProblemOrchestrator:
    """Orchestrates execution against a snapshot produced by confirmation."""

    model_execution_coordinator: ModelExecutionCoordinator

    def execute(
            self,
            confirmed_snapshot: ProblemSnapshot,
    ) -> ExecutionRunContext:
        """Execute three models against one confirmed snapshot."""
        if confirmed_snapshot.version < 1:
            raise ValueError(
                "A positive confirmed snapshot version is required."
            )

        if not confirmed_snapshot.problem_id.strip():
            raise ValueError("Problem identifier cannot be empty.")

        adapters = (
            self.model_execution_coordinator.create_adapters()
        )

        model_ids = tuple(
            adapter.get_model_identifier()
            for adapter in adapters
        )

        if len(model_ids) != 3 or len(set(model_ids)) != 3:
            raise ValueError(
                "Exactly three distinct models are required."
            )

        run = ExecutionRunContract(
            run_id=str(uuid4()),
            snapshot=confirmed_snapshot,
            model_ids=model_ids,
            status=ExecutionStatus.PENDING,
        )

        context = ExecutionRunContext(run)
        context.start()

        try:
            responses = self.model_execution_coordinator.execute(
                context.run,
                adapters,
            )
            context.record_responses(responses)
        except Exception:
            if context.run.status == ExecutionStatus.RUNNING:
                context.fail()
            raise

        return context


class ExecutionFacade:
    """Entry point for execution, results, and verification."""

    problem_orchestrator: ProblemOrchestrator
    result_collector: ExecutionResultCollector
    verification_coordinator: VerificationCoordinator

    def start_execution(
            self,
            confirmed_snapshot: ProblemSnapshot,
    ) -> ExecutionRunContext:
        """Start a new execution through the problem orchestrator."""
        return self.problem_orchestrator.execute(
            confirmed_snapshot
        )

    def get_results(
            self,
            execution_run: ExecutionRunContext,
    ) -> tuple[ModelExecution, ModelExecution, ModelExecution]:
        """Return all model outcomes, including failed ones."""
        return self.result_collector.collect(
            execution_run
        )

    def get_verification_results(
            self,
            execution_run: ExecutionRunContext,
    ) -> list[VerificationResult]:
        """Verify all available completed responses."""
        responses = [
            item.response
            for item in self.get_results(execution_run)
            if (
                    item.response is not None
                    and item.status == ModelStatus.COMPLETED
            )
        ]

        return self.verification_coordinator.verify(
            execution_run,
            responses,
        )
