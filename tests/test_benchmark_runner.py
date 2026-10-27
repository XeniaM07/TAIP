
"""Tests for the benchmark Template Method."""

import unittest

from taip.benchmark.models import BenchmarkDataset, BenchmarkResult
from taip.benchmark.observer import (
    BenchmarkEvent,
    BenchmarkProgressObserver,
    BenchmarkResultPublisher,
)
from taip.benchmark.runner import BenchmarkRunner


class FakeBenchmarkRunner(BenchmarkRunner):
    """A minimal runner used to test the common workflow."""

    def __init__(self, should_fail: bool = False) -> None:
        self.steps: list[str] = []
        self.should_fail = should_fail

        super().__init__(
            benchmark_id="math-01",
            model_id="model-01",
            result_publisher=BenchmarkResultPublisher(),
        )

    def load_dataset(self) -> BenchmarkDataset:
        self.steps.append("load_dataset")
        return BenchmarkDataset(
            dataset_id="dataset-01",
            version="1.0",
            cases=(),
        )

    def execute_cases(self) -> None:
        self.steps.append("execute_cases")
        if self.should_fail:
            raise RuntimeError("Simulated benchmark failure")

    def evaluate_results(self) -> BenchmarkResult:
        self.steps.append("evaluate_results")
        return BenchmarkResult(
            benchmark_id="math-01",
            dataset_version="1.0",
            model_id="model-01",
            total_cases=0,
            successful_cases=0,
            score=None,
        )

    def generate_report(self) -> None:
        self.steps.append("generate_report")


class BenchmarkRunnerTests(unittest.TestCase):
    def test_workflow_executes_steps_in_order(self) -> None:
        runner = FakeBenchmarkRunner()

        runner.run_benchmark()

        self.assertEqual(
            runner.steps,
            [
                "load_dataset",
                "execute_cases",
                "evaluate_results",
                "generate_report",
            ],
        )

    def test_success_publishes_running_and_completed_events(self) -> None:
        runner = FakeBenchmarkRunner()
        observer = BenchmarkProgressObserver()
        runner.result_publisher.attach(observer)

        runner.run_benchmark()

        self.assertEqual(
            [event.state for event in observer.events],
            ["running", "completed"],
        )
        self.assertIsNotNone(observer.events[-1].result)

    def test_failure_publishes_failed_event_and_propagates_error(self) -> None:
        runner = FakeBenchmarkRunner(should_fail=True)
        observer = BenchmarkProgressObserver()
        runner.result_publisher.attach(observer)

        with self.assertRaisesRegex(
            RuntimeError, "Simulated benchmark failure"
        ):
            runner.run_benchmark()

        self.assertEqual(
            [event.state for event in observer.events],
            ["running", "failed"],
        )

    def test_completed_event_has_execution_id(self) -> None:
        runner = FakeBenchmarkRunner()
        observer = BenchmarkProgressObserver()
        runner.result_publisher.attach(observer)

        runner.run_benchmark()

        events: list[BenchmarkEvent] = observer.events
        self.assertTrue(events[0].execution_id)
        self.assertEqual(
            events[0].execution_id,
            events[1].execution_id,
        )


if __name__ == "__main__":
    unittest.main()
