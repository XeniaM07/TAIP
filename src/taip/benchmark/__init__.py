"""Benchmark and comparison APIs."""

from taip.benchmark.models import (
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkResult,
    ConstraintExtractionMetrics,
    EditTypeCount,
    TimetableComparisonReport,
    TimetableModelMetrics,
    TimetableReferenceCase,
    VerificationAuditMetrics,
)
from taip.benchmark.observer import (
    BenchmarkEvent,
    BenchmarkProgressObserver,
    BenchmarkResultCollector,
    BenchmarkResultPublisher,
    TimetableComparisonCollector,
)
from taip.benchmark.repository import InMemoryBenchmarkRepository
from taip.benchmark.reference_data import (
    TIMETABLE_REFERENCE_CASES,
    get_reference_case,
)
from taip.benchmark.timetable_service import TimetableBenchmarkService

__all__ = [
    "BenchmarkCase",
    "BenchmarkDataset",
    "BenchmarkEvent",
    "BenchmarkProgressObserver",
    "BenchmarkResult",
    "BenchmarkResultCollector",
    "BenchmarkResultPublisher",
    "ConstraintExtractionMetrics",
    "EditTypeCount",
    "InMemoryBenchmarkRepository",
    "TimetableBenchmarkService",
    "TimetableComparisonCollector",
    "TimetableComparisonReport",
    "TimetableModelMetrics",
    "TimetableReferenceCase",
    "TIMETABLE_REFERENCE_CASES",
    "VerificationAuditMetrics",
    "get_reference_case",
]
