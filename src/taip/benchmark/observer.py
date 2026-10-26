
"""Observer pattern structure for benchmark notifications."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from taip.benchmark.models import BenchmarkResult


@dataclass(frozen=True, slots=True)
class BenchmarkEvent:
    """Represents a benchmark execution event."""

    benchmark_id: str
    execution_id: str
    state: str
    result: BenchmarkResult | None


class BenchmarkObserver(ABC):
    """Abstract interface for components observing benchmark events."""

    @abstractmethod
    def update(self, event: BenchmarkEvent) -> None:
        """Declare the observer notification method."""
        ...


class BenchmarkSubject(ABC):
    """Abstract interface for managing benchmark observers."""

    observers: list[BenchmarkObserver]

    @abstractmethod
    def attach(self, observer: BenchmarkObserver) -> None:
        """Declare observer registration."""
        ...

    @abstractmethod
    def detach(self, observer: BenchmarkObserver) -> None:
        """Declare observer removal."""
        ...

    @abstractmethod
    def notify_observers(self, event: BenchmarkEvent) -> None:
        """Declare notification of registered observers."""
        ...


class BenchmarkResultPublisher(BenchmarkSubject):
    """Publishes benchmark execution events to registered observers."""

    observers: list[BenchmarkObserver]

    def attach(self, observer: BenchmarkObserver) -> None:
        ...

    def detach(self, observer: BenchmarkObserver) -> None:
        ...

    def notify_observers(self, event: BenchmarkEvent) -> None:
        ...


class BenchmarkResultCollector(BenchmarkObserver):
    """Observes benchmark events related to results."""

    def update(self, event: BenchmarkEvent) -> None:
        ...


class BenchmarkProgressObserver(BenchmarkObserver):
    """Observes benchmark execution progress and state changes."""

    def update(self, event: BenchmarkEvent) -> None:
        ...
