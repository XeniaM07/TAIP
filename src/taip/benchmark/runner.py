
"""Template Method for benchmark evaluation."""

from abc import ABC, abstractmethod
from uuid import uuid4
from collections.abc import Callable

from taip.benchmark.models import BenchmarkDataset, BenchmarkResult
from taip.benchmark.observer import (
    BenchmarkEvent,
    BenchmarkSubject,
)


class BenchmarkRunner(ABC):
    """Defines the common benchmark execution workflow."""

    def __init__(
        self,
        benchmark_id: str,
        model_id: str,
        result_publisher: BenchmarkSubject,
    ) -> None:
        self.benchmark_id = benchmark_id
        self.model_id = model_id
        self.result_publisher = result_publisher
        self.dataset: BenchmarkDataset | None = None

    def run_benchmark(self) -> BenchmarkResult:
        """Execute the benchmark workflow in a fixed order."""
        execution_id = str(uuid4())

        self.result_publisher.notify_observers(
            BenchmarkEvent(
                benchmark_id=self.benchmark_id,
                execution_id=execution_id,
                state="running",
                result=None,
            )
        )

        try:
            self.dataset = self.load_dataset()
            self.execute_cases()
            result = self.evaluate_results()
            self.generate_report()
        except Exception:
            self.result_publisher.notify_observers(
                BenchmarkEvent(
                    benchmark_id=self.benchmark_id,
                    execution_id=execution_id,
                    state="failed",
                    result=None,
                )
            )
            raise

        self.result_publisher.notify_observers(
            BenchmarkEvent(
                benchmark_id=self.benchmark_id,
                execution_id=execution_id,
                state="completed",
                result=result,
            )
        )
        return result

    @abstractmethod
    def load_dataset(self) -> BenchmarkDataset:
        """Load the dataset used for evaluation."""

    @abstractmethod
    def execute_cases(self) -> None:
        """Execute the benchmark cases."""

    @abstractmethod
    def evaluate_results(self) -> BenchmarkResult:
        """Calculate the benchmark result."""

    @abstractmethod
    def generate_report(self) -> None:
        """Generate the benchmark report."""


class MathematicalBenchmarkRunner(BenchmarkRunner):
    """Concrete benchmark runner using a supplied dataset and solver."""

    source_dataset: BenchmarkDataset
    solver: Callable[[str], str]

    def load_dataset(self) -> BenchmarkDataset:
        """Load the configured mathematical benchmark dataset."""
        if not hasattr(self, "source_dataset"):
            raise ValueError("No benchmark dataset was configured.")

        if not isinstance(self.source_dataset, BenchmarkDataset):
            raise TypeError("Expected a BenchmarkDataset.")

        return self.source_dataset

    def execute_cases(self) -> None:
        """Execute every case and collect model outputs."""
        if self.dataset is None:
            raise RuntimeError("Dataset must be loaded first.")

        if not hasattr(self, "solver") or not callable(self.solver):
            raise ValueError("No benchmark solver was configured.")

        self.case_results: list[dict[str, object]] = []

        for case in self.dataset.cases:
            actual = self.solver(case.problem_statement)

            # Accept harmless whitespace differences.
            correct = (
                isinstance(actual, str)
                and actual.strip() == case.expected_result.strip()
            )

            self.case_results.append(
                {
                    "case_id": case.case_id,
                    "expected": case.expected_result,
                    "actual": actual,
                    "correct": correct,
                }
            )

    def evaluate_results(self) -> BenchmarkResult:
        """Compute exact-match accuracy for the benchmark."""
        if self.dataset is None:
            raise RuntimeError("Dataset must be loaded first.")

        if not hasattr(self, "case_results"):
            raise RuntimeError("Cases must be executed first.")

        total = len(self.dataset.cases)
        successful = sum(
            1 for entry in self.case_results if entry["correct"]
        )

        score = successful / total if total > 0 else None

        self.last_result = BenchmarkResult(
            benchmark_id=self.benchmark_id,
            dataset_version=self.dataset.version,
            model_id=self.model_id,
            total_cases=total,
            successful_cases=successful,
            score=score,
        )
        return self.last_result

    def generate_report(self) -> None:
        """Build an in-memory benchmark report."""
        if not hasattr(self, "last_result"):
            raise RuntimeError("Results must be evaluated first.")

        result = self.last_result

        self.report = {
            "benchmark_id": result.benchmark_id,
            "dataset_version": result.dataset_version,
            "model_id": result.model_id,
            "total_cases": result.total_cases,
            "successful_cases": result.successful_cases,
            "score": result.score,
            "cases": list(self.case_results),
        }