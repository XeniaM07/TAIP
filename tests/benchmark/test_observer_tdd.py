"""TDD tests for reliable benchmark result collection."""

import unittest

from taip.benchmark.models import BenchmarkResult
from taip.benchmark.observer import (
    BenchmarkEvent,
    BenchmarkProgressObserver,
    BenchmarkResultCollector,
    BenchmarkResultPublisher,
)


class BenchmarkObserverTDDTests(unittest.TestCase):

    def setUp(self):
        self.publisher = BenchmarkResultPublisher()
        self.collector = BenchmarkResultCollector()
        self.progress = BenchmarkProgressObserver()

        self.publisher.attach(self.collector)
        self.publisher.attach(self.progress)

        self.result = BenchmarkResult(
            benchmark_id="math-01",
            dataset_version="1.0",
            model_id="model-01",
            total_cases=10,
            successful_cases=8,
            score=0.8,
        )

    def publish(self, execution_id, state, result=None):
        event = BenchmarkEvent(
            benchmark_id="math-01",
            execution_id=execution_id,
            state=state,
            result=result,
        )
        self.publisher.notify_observers(event)
        return event

    def test_completed_result_is_collected(self):
        self.publish("run-01", "completed", self.result)
        self.assertEqual(self.collector.results, [self.result])

    def test_running_event_does_not_collect_result(self):
        self.publish("run-01", "running", self.result)
        self.assertEqual(self.collector.results, [])

    def test_failed_event_does_not_collect_result(self):
        self.publish("run-01", "failed", self.result)
        self.assertEqual(self.collector.results, [])

    def test_duplicate_completion_is_collected_once(self):
        self.publish("run-01", "completed", self.result)
        self.publish("run-01", "completed", self.result)

        self.assertEqual(self.collector.results, [self.result])

    def test_different_executions_are_both_collected(self):
        self.publish("run-01", "completed", self.result)
        self.publish("run-02", "completed", self.result)

        self.assertEqual(
            self.collector.results,
            [self.result, self.result],
        )

    def test_progress_observer_keeps_all_events(self):
        first = self.publish("run-01", "running")
        second = self.publish(
            "run-01", "completed", self.result
        )

        self.assertEqual(self.progress.events, [first, second])

    def test_detached_collector_receives_no_events(self):
        self.publisher.detach(self.collector)
        self.publish("run-01", "completed", self.result)

        self.assertEqual(self.collector.results, [])


if __name__ == "__main__":
    unittest.main()