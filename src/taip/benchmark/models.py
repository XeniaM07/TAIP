
"""Data structures for independent benchmark evaluation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """Represents one test case in a mathematical benchmark."""

    case_id: str
    problem_statement: str
    expected_result: str


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    """Represents a versioned collection of benchmark cases."""

    dataset_id: str
    version: str
    cases: tuple[BenchmarkCase, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Represents the result of evaluating a model on a benchmark."""

    benchmark_id: str
    dataset_version: str
    model_id: str
    total_cases: int
    successful_cases: int
    score: float | None
