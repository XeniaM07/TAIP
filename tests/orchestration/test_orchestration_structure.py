"""Structural tests for orchestration components."""

import inspect
import unittest
from typing import get_type_hints

from taip.common.contracts import (
    ExecutionRun as ExecutionRunContract,
    ExecutionStatus,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
)
from taip.orchestration.execution import (
    ExecutionResultCollector,
    ExecutionRunContext,
    ModelExecution,
    VerificationCoordinator,
)
from taip.orchestration.facade import (
    ExecutionFacade,
    ModelExecutionCoordinator,
    ProblemOrchestrator,
)
from taip.orchestration.states import (
    CompletedState,
    ExecutionState,
    FailedState,
    PendingState,
    RunningState,
)


class OrchestrationStructureTests(unittest.TestCase):
    def test_execution_state_is_abstract_and_states_implement_it(self) -> None:
        self.assertTrue(inspect.isabstract(ExecutionState))

        for state_type in (
            PendingState,
            RunningState,
            CompletedState,
            FailedState,
        ):
            with self.subTest(state=state_type.__name__):
                self.assertTrue(issubclass(state_type, ExecutionState))
                self.assertFalse(inspect.isabstract(state_type))
                for method in ("status", "start", "complete", "fail"):
                    self.assertIn(method, state_type.__dict__)

    def test_state_interface_uses_execution_status_contract(self) -> None:
        self.assertEqual(
            get_type_hints(ExecutionState.status.fget),
            {"return": ExecutionStatus},
        )
        for method in ("start", "complete", "fail"):
            with self.subTest(method=method):
                self.assertEqual(
                    get_type_hints(getattr(ExecutionState, method)),
                    {"return": type(None)},
                )

    def test_model_execution_uses_shared_contracts(self) -> None:
        self.assertEqual(
            get_type_hints(ModelExecution),
            {
                "execution_run": ExecutionRunContract,
                "model_id": str,
                "status": ModelStatus,
                "response": ModelResponse | None,
            },
        )

    def test_run_context_declares_state_and_three_model_executions(self) -> None:
        self.assertEqual(
            get_type_hints(ExecutionRunContext),
            {
                "run": ExecutionRunContract,
                "current_state": ExecutionState,
                "model_executions": tuple[
                    ModelExecution,
                    ModelExecution,
                    ModelExecution,
                ],
            },
        )
        for method in ("start", "complete", "fail"):
            with self.subTest(method=method):
                self.assertTrue(callable(getattr(ExecutionRunContext, method)))

    def test_collect_and_verify_signatures_use_shared_contracts(self) -> None:
        self.assertEqual(
            get_type_hints(ExecutionResultCollector.collect),
            {
                "execution_run": ExecutionRunContext,
                "return": list[ModelResponse],
            },
        )
        self.assertEqual(
            get_type_hints(VerificationCoordinator.verify),
            {
                "execution_run": ExecutionRunContext,
                "responses": list[ModelResponse],
                "return": list[VerificationResult],
            },
        )

    def test_facade_components_use_shared_contracts(self) -> None:
        self.assertEqual(
            get_type_hints(ModelExecutionCoordinator.execute),
            {
                "execution_run": ExecutionRunContract,
                "return": tuple[ModelResponse, ModelResponse, ModelResponse],
            },
        )
        self.assertEqual(
            get_type_hints(ProblemOrchestrator.execute),
            {
                "confirmed_snapshot": ProblemSnapshot,
                "return": ExecutionRunContext,
            },
        )
        self.assertEqual(
            get_type_hints(ExecutionFacade),
            {
                "problem_orchestrator": ProblemOrchestrator,
                "result_collector": ExecutionResultCollector,
                "verification_coordinator": VerificationCoordinator,
            },
        )
        self.assertEqual(
            get_type_hints(ExecutionFacade.start_execution),
            {
                "confirmed_snapshot": ProblemSnapshot,
                "return": ExecutionRunContext,
            },
        )
        self.assertEqual(
            get_type_hints(ExecutionFacade.get_results),
            {
                "execution_run": ExecutionRunContext,
                "return": list[ModelResponse],
            },
        )
        self.assertEqual(
            get_type_hints(ExecutionFacade.get_verification_results),
            {
                "execution_run": ExecutionRunContext,
                "return": list[VerificationResult],
            },
        )


if __name__ == "__main__":
    unittest.main()
