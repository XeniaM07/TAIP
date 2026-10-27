
"""Template Method for benchmark evaluation."""

from abc import ABC, abstractmethod
from uuid import uuid4

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
    """Runs a benchmark using mathematical test cases."""

    def load_dataset(self) -> BenchmarkDataset:
        raise NotImplementedError(
            "Mathematical dataset loading must be implemented."
        )

    def execute_cases(self) -> None:
        raise NotImplementedError(
            "Mathematical case execution must be implemented."
        )

    def evaluate_results(self) -> BenchmarkResult:
        raise NotImplementedError(
            "Mathematical result evaluation must be implemented."
        )

    def generate_report(self) -> None:
        raise NotImplementedError(
            "Mathematical report generation must be implemented."
        )
