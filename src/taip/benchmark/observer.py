
"""Observer pattern for benchmark execution notifications."""

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
    """Interface implemented by components observing benchmark events."""

    @abstractmethod
    def update(self, event: BenchmarkEvent) -> None:
        """Handle a benchmark event."""
        ...


class BenchmarkSubject(ABC):
    """Interface for managing benchmark observers."""

    @abstractmethod
    def attach(self, observer: BenchmarkObserver) -> None:
        """Register an observer."""
        ...

    @abstractmethod
    def detach(self, observer: BenchmarkObserver) -> None:
        """Unregister an observer."""
        ...

    @abstractmethod
    def notify_observers(self, event: BenchmarkEvent) -> None:
        """Notify all registered observers."""
        ...


class BenchmarkResultPublisher(BenchmarkSubject):
    """Publishes benchmark events to registered observers."""

    def __init__(self) -> None:
        self.observers: list[BenchmarkObserver] = []

    def attach(self, observer: BenchmarkObserver) -> None:
        if observer not in self.observers:
            self.observers.append(observer)

    def detach(self, observer: BenchmarkObserver) -> None:
        if observer in self.observers:
            self.observers.remove(observer)

    def notify_observers(self, event: BenchmarkEvent) -> None:
        for observer in tuple(self.observers):
            observer.update(event)


class BenchmarkResultCollector(BenchmarkObserver):
    """Collects benchmark results received through notifications."""

    def __init__(self) -> None:
        self.results: list[BenchmarkResult] = []

    def update(self, event: BenchmarkEvent) -> None:
        if event.result is not None:
            self.results.append(event.result)


class BenchmarkProgressObserver(BenchmarkObserver):
    """Records benchmark execution states."""

    def __init__(self) -> None:
        self.events: list[BenchmarkEvent] = []

    def update(self, event: BenchmarkEvent) -> None:
        self.events.append(event)
