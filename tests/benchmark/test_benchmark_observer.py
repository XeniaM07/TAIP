
"""Tests for the benchmark Observer pattern."""

import unittest

from taip.benchmark.models import BenchmarkResult
from taip.benchmark.observer import (
    BenchmarkEvent,
    BenchmarkProgressObserver,
    BenchmarkResultCollector,
    BenchmarkResultPublisher,
)


class BenchmarkObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.publisher = BenchmarkResultPublisher()
        self.event = BenchmarkEvent(
            benchmark_id="math-01",
            execution_id="run-01",
            state="completed",
            result=BenchmarkResult(
                benchmark_id="math-01",
                dataset_version="1.0",
                model_id="model-01",
                total_cases=10,
                successful_cases=8,
                score=0.8,
            ),
        )

    def test_attach_registers_observer(self) -> None:
        observer = BenchmarkProgressObserver()

        self.publisher.attach(observer)

        self.assertIn(observer, self.publisher.observers)

    def test_attach_does_not_register_duplicate(self) -> None:
        observer = BenchmarkProgressObserver()

        self.publisher.attach(observer)
        self.publisher.attach(observer)

        self.assertEqual(self.publisher.observers.count(observer), 1)

    def test_detach_removes_observer(self) -> None:
        observer = BenchmarkProgressObserver()
        self.publisher.attach(observer)

        self.publisher.detach(observer)

        self.assertNotIn(observer, self.publisher.observers)

    def test_notify_sends_event_to_observers(self) -> None:
        observer = BenchmarkProgressObserver()
        self.publisher.attach(observer)

        self.publisher.notify_observers(self.event)

        self.assertEqual(observer.events, [self.event])

    def test_result_collector_stores_available_result(self) -> None:
        collector = BenchmarkResultCollector()
        self.publisher.attach(collector)

        self.publisher.notify_observers(self.event)

        self.assertEqual(collector.results, [self.event.result])

    def test_result_collector_ignores_event_without_result(self) -> None:
        collector = BenchmarkResultCollector()
        event = BenchmarkEvent(
            benchmark_id="math-01",
            execution_id="run-02",
            state="running",
            result=None,
        )

        self.publisher.attach(collector)
        self.publisher.notify_observers(event)

        self.assertEqual(collector.results, [])

    def test_detaching_unknown_observer_does_not_raise(self) -> None:
        observer = BenchmarkProgressObserver()

        self.publisher.detach(observer)

        self.assertEqual(self.publisher.observers, [])


if __name__ == "__main__":
    unittest.main()
