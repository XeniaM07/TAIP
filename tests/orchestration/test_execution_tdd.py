"""TDD tests for executing three AI models in one run."""

import unittest
from unittest.mock import Mock

from taip.common.contracts import (
    ExecutionRun,
    ExecutionStatus,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
)
from taip.orchestration.facade import ModelExecutionCoordinator


class ModelExecutionTDDTests(unittest.TestCase):

    def setUp(self):
        self.snapshot = ProblemSnapshot(
            problem_id="problem-1",
            version=1,
            original_prompt="Solve x + 2 = 5",
            objective="Find x",
            constraints=(),
        )

        self.run = ExecutionRun(
            run_id="run-123",
            snapshot=self.snapshot,
            model_ids=("model-1", "model-2", "model-3"),
            status=ExecutionStatus.RUNNING,
        )

        self.coordinator = ModelExecutionCoordinator()

        self.adapters = tuple(
            self.make_adapter(model_id)
            for model_id in self.run.model_ids
        )

    def make_adapter(self, model_id):
        adapter = Mock()
        adapter.get_model_identifier.return_value = model_id
        adapter.generate_solution.return_value = ModelResponse(
            model_id=model_id,
            run_id=self.run.run_id,
            output="x = 3",
            status=ModelStatus.COMPLETED,
        )
        return adapter

    def test_executes_all_three_adapters(self):
        responses = self.coordinator.execute(
            self.run, self.adapters
        )

        self.assertEqual(len(responses), 3)

        for adapter in self.adapters:
            adapter.generate_solution.assert_called_once()

    def test_all_requests_share_the_same_snapshot(self):
        self.coordinator.execute(self.run, self.adapters)

        for adapter in self.adapters:
            args, kwargs = adapter.generate_solution.call_args

            self.assertIs(args[0].snapshot, self.snapshot)
            self.assertEqual(kwargs["run_id"], "run-123")

    def test_responses_preserve_model_order(self):
        responses = self.coordinator.execute(
            self.run, self.adapters
        )

        self.assertEqual(
            tuple(r.model_id for r in responses),
            self.run.model_ids,
        )

    def test_one_model_failure_does_not_stop_others(self):
        self.adapters[1].generate_solution.side_effect = (
            RuntimeError("Provider unavailable")
        )

        responses = self.coordinator.execute(
            self.run, self.adapters
        )

        self.assertEqual(
            tuple(r.status for r in responses),
            (
                ModelStatus.COMPLETED,
                ModelStatus.FAILED,
                ModelStatus.COMPLETED,
            ),
        )
        self.adapters[2].generate_solution.assert_called_once()

    def test_failed_model_preserves_run_and_model_ids(self):
        self.adapters[1].generate_solution.side_effect = (
            RuntimeError("Provider unavailable")
        )

        responses = self.coordinator.execute(
            self.run, self.adapters
        )

        self.assertEqual(responses[1].model_id, "model-2")
        self.assertEqual(responses[1].run_id, "run-123")
        self.assertIsNone(responses[1].output)

    def test_rejects_wrong_number_of_adapters(self):
        with self.assertRaises(ValueError):
            self.coordinator.execute(
                self.run, self.adapters[:2]
            )

    def test_rejects_mismatched_model_identifiers(self):
        self.adapters[0].get_model_identifier.return_value = (
            "wrong-model"
        )

        with self.assertRaises(ValueError):
            self.coordinator.execute(
                self.run, self.adapters
            )


if __name__ == "__main__":
    unittest.main()