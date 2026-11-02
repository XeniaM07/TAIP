
"""End-to-end integration of problem, AI, verification and benchmark."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from taip.ai_integration.creators import (
    OpenAIAdapterCreator,
    GeminiAdapterCreator,
    ThirdModelAdapterCreator,
)
from taip.benchmark.models import BenchmarkCase, BenchmarkDataset
from taip.benchmark.observer import (
    BenchmarkProgressObserver,
    BenchmarkResultPublisher,
)
from taip.benchmark.runner import MathematicalBenchmarkRunner
from taip.common.contracts import (
    ExecutionStatus,
    ModelStatus,
    VerificationStatus,
)
from taip.mathematical_verification.verification_chain import (
    InputValidationHandler,
    ConstraintVerificationHandler,
    MathematicalVerificationHandler,
)
from taip.orchestration.execution import (
    ExecutionResultCollector,
    VerificationCoordinator,
)
from taip.orchestration.facade import (
    ExecutionFacade,
    ModelExecutionCoordinator,
    ProblemOrchestrator,
)
from taip.problem_management.problem import MathematicalProblem
from taip.problem_management.version_history import ProblemVersionHistory


class EndToEndIntegrationTests(unittest.TestCase):

    @staticmethod
    def solve_prompt(text):
        """Deterministic fake provider for supported equations."""
        if "x + 2 = 5" in text:
            return "x = 3"
        if "x - 4 = 2" in text:
            return "x = 6"
        if "x = 8" in text:
            return "x = 8"
        raise ValueError("Unknown test problem")

    def setUp(self):
        openai_client = Mock()
        openai_client.responses.create.side_effect = (
            lambda **kwargs: SimpleNamespace(
                output_text=self.solve_prompt(kwargs["input"])
            )
        )

        gemini_client = Mock()
        gemini_client.models.generate_content.side_effect = (
            lambda **kwargs: SimpleNamespace(
                text=self.solve_prompt(kwargs["contents"])
            )
        )

        third_client = Mock()
        third_client.generate.side_effect = (
            lambda **kwargs: self.solve_prompt(kwargs["prompt"])
        )

        creators = []

        for creator_type, model_id, client in (
            (OpenAIAdapterCreator, "openai-test", openai_client),
            (GeminiAdapterCreator, "gemini-test", gemini_client),
            (ThirdModelAdapterCreator, "third-test", third_client),
        ):
            creator = creator_type()
            creator.model_id = model_id
            creator.client = client
            creators.append(creator)

        coordinator = ModelExecutionCoordinator()
        coordinator.adapter_creators = tuple(creators)

        orchestrator = ProblemOrchestrator()
        orchestrator.model_execution_coordinator = coordinator

        validation = InputValidationHandler()
        constraints = ConstraintVerificationHandler()
        mathematics = MathematicalVerificationHandler()

        validation.set_next(constraints)
        constraints.set_next(mathematics)

        verifier = VerificationCoordinator()
        verifier.verification_chain = validation

        self.facade = ExecutionFacade()
        self.facade.problem_orchestrator = orchestrator
        self.facade.result_collector = ExecutionResultCollector()
        self.facade.verification_coordinator = verifier

    def confirm_problem(self, statement, problem_id):
        problem = MathematicalProblem()
        problem.problem_id = problem_id
        problem.original_prompt = statement
        problem.objective = "Find x"
        problem.constraints = []
        problem.version_history = ProblemVersionHistory()

        snapshot = problem.confirm(version=1)

        self.assertIs(
            problem.version_history.get_snapshot(problem_id, 1),
            snapshot,
        )
        return snapshot

    def test_confirmed_problem_reaches_three_models_and_verifier(self):
        snapshot = self.confirm_problem(
            "Solve x + 2 = 5", "problem-1"
        )

        context = self.facade.start_execution(snapshot)
        responses = self.facade.get_results(context)
        verification = self.facade.get_verification_results(context)

        self.assertEqual(
            context.run.status, ExecutionStatus.COMPLETED
        )
        self.assertIs(context.run.snapshot, snapshot)

        self.assertEqual(len(responses), 3)
        self.assertTrue(
            all(
                result.status == ModelStatus.COMPLETED
                for result in responses
            )
        )
        self.assertEqual(
            [result.response.output for result in responses],
            ["x = 3", "x = 3", "x = 3"],
        )

        self.assertEqual(len(verification), 3)
        self.assertTrue(
            all(
                item.status == VerificationStatus.VERIFIED
                for item in verification
            )
        )

    def test_benchmark_uses_integrated_execution(self):
        dataset = BenchmarkDataset(
            dataset_id="linear-equations",
            version="1.0",
            cases=(
                BenchmarkCase(
                    "case-1", "Solve x + 2 = 5", "x = 3"
                ),
                BenchmarkCase(
                    "case-2", "Solve x - 4 = 2", "x = 6"
                ),
                BenchmarkCase(
                    "case-3", "Solve x = 8", "x = 8"
                ),
            ),
        )

        def integrated_solver(statement):
            snapshot = self.confirm_problem(
                statement,
                problem_id=f"benchmark-{statement}",
            )
            context = self.facade.start_execution(snapshot)

            verification = self.facade.get_verification_results(
                context
            )
            if not all(
                item.status == VerificationStatus.VERIFIED
                for item in verification
            ):
                raise ValueError("Mathematical verification failed")

            results = self.facade.get_results(context)
            return results[0].response.output

        publisher = BenchmarkResultPublisher()
        observer = BenchmarkProgressObserver()
        publisher.attach(observer)

        benchmark = MathematicalBenchmarkRunner(
            benchmark_id="integrated-benchmark",
            model_id="openai-test",
            result_publisher=publisher,
        )
        benchmark.source_dataset = dataset
        benchmark.solver = integrated_solver

        result = benchmark.run_benchmark()

        self.assertEqual(result.total_cases, 3)
        self.assertEqual(result.successful_cases, 3)
        self.assertEqual(result.score, 1.0)

        self.assertEqual(
            [event.state for event in observer.events],
            ["running", "completed"],
        )
        self.assertEqual(
            benchmark.report["successful_cases"], 3
        )


if __name__ == "__main__":
    unittest.main()