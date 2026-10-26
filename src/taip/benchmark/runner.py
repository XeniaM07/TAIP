
"""Template Method structure for benchmark evaluation."""

from abc import ABC, abstractmethod

from taip.benchmark.models import (
    BenchmarkDataset,
    BenchmarkResult,
)
from taip.benchmark.observer import BenchmarkSubject


class BenchmarkRunner(ABC):
    """Defines the benchmark workflow and its notification dependency."""

    dataset: BenchmarkDataset
    result_publisher: BenchmarkSubject

    def run_benchmark(self) -> BenchmarkResult:
        """Declare the common benchmark workflow."""
        ...

    @abstractmethod
    def load_dataset(self) -> BenchmarkDataset:
        """Declare the dataset loading step."""
        ...

    @abstractmethod
    def execute_cases(self) -> None:
        """Declare the benchmark case execution step."""
        ...

    @abstractmethod
    def evaluate_results(self) -> BenchmarkResult:
        """Declare the result evaluation step."""
        ...

    @abstractmethod
    def generate_report(self) -> None:
        """Declare the report generation step."""
        ...


class MathematicalBenchmarkRunner(BenchmarkRunner):
    """Specializes the workflow for mathematical benchmark datasets."""

    dataset: BenchmarkDataset
    result_publisher: BenchmarkSubject

    def load_dataset(self) -> BenchmarkDataset:
        ...

    def execute_cases(self) -> None:
        ...

    def evaluate_results(self) -> BenchmarkResult:
        ...

    def generate_report(self) -> None:
        ...
