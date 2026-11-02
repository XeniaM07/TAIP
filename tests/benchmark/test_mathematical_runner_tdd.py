"""TDD tests for the concrete mathematical benchmark runner."""

import unittest
from unittest.mock import Mock

from taip.benchmark.models import BenchmarkCase, BenchmarkDataset
from taip.benchmark.observer import (
    BenchmarkProgressObserver,
    BenchmarkResultPublisher,
)
from taip.benchmark.runner import MathematicalBenchmarkRunner


class MathematicalBenchmarkRunnerTDDTests(unittest.TestCase):

    def setUp(self):
        self.dataset = BenchmarkDataset(
            dataset_id="linear-equations",
            version="1.0",
            cases=(
                BenchmarkCase("case-1", "Solve x + 2 = 5", "x = 3"),
                BenchmarkCase("case-2", "Solve x - 4 = 2", "x = 6"),
                BenchmarkCase("case-3", "Solve x = 8", "x = 8"),
            ),
        )

        self.solver = Mock(
            side_effect=["x = 3", "x = 6", "x = 9"]
        )
        self.publisher = BenchmarkResultPublisher()

        self.runner = MathematicalBenchmarkRunner(
            benchmark_id="math-benchmark",
            model_id="model-01",
            result_publisher=self.publisher,
        )
        self.runner.source_dataset = self.dataset
        self.runner.solver = self.solver

    def test_loads_supplied_dataset(self):
        loaded = self.runner.load_dataset()

        self.assertIs(loaded, self.dataset)

    def test_executes_each_case_once(self):
        self.runner.run_benchmark()

        self.assertEqual(self.solver.call_count, 3)
        self.assertEqual(
            self.solver.call_args_list[0].args[0],
            "Solve x + 2 = 5",
        )

    def test_counts_correct_responses(self):
        result = self.runner.run_benchmark()

        self.assertEqual(result.total_cases, 3)
        self.assertEqual(result.successful_cases, 2)
        self.assertAlmostEqual(result.score, 2 / 3)

    def test_result_contains_metadata(self):
        result = self.runner.run_benchmark()

        self.assertEqual(result.benchmark_id, "math-benchmark")
        self.assertEqual(result.dataset_version, "1.0")
        self.assertEqual(result.model_id, "model-01")

    def test_generates_structured_report(self):
        self.runner.run_benchmark()

        self.assertEqual(
            self.runner.report["benchmark_id"],
            "math-benchmark",
        )
        self.assertEqual(self.runner.report["total_cases"], 3)
        self.assertEqual(self.runner.report["successful_cases"], 2)

    def test_empty_dataset_has_no_score(self):
        self.runner.source_dataset = BenchmarkDataset(
            dataset_id="empty",
            version="2.0",
            cases=(),
        )

        result = self.runner.run_benchmark()

        self.assertEqual(result.total_cases, 0)
        self.assertEqual(result.successful_cases, 0)
        self.assertIsNone(result.score)
        self.solver.assert_not_called()

    def test_notifies_observers(self):
        observer = BenchmarkProgressObserver()
        self.publisher.attach(observer)

        result = self.runner.run_benchmark()

        self.assertEqual(
            [event.state for event in observer.events],
            ["running", "completed"],
        )
        self.assertIs(observer.events[-1].result, result)

    def test_solver_failure_propagates_and_notifies(self):
        self.runner.solver = Mock(
            side_effect=RuntimeError("Solver unavailable")
        )
        observer = BenchmarkProgressObserver()
        self.publisher.attach(observer)

        with self.assertRaisesRegex(
            RuntimeError, "Solver unavailable"
        ):
            self.runner.run_benchmark()

        self.assertEqual(
            [event.state for event in observer.events],
            ["running", "failed"],
        )


if __name__ == "__main__":
    unittest.main()