"""Minimal structural checks for the shared Lab 04 contracts."""

import unittest
from dataclasses import FrozenInstanceError

from taip.common.contracts import (
    Constraint,
    ExecutionRun,
    ExecutionStatus,
    ModelRequest,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
    VerificationStatus,
)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = ProblemSnapshot(
            problem_id="p-1",
            version=1,
            original_prompt="Solve x = 2",
            objective="Find x",
            constraints=(Constraint("c-1", "x = 2"),),
        )

    def test_snapshot_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.snapshot.version = 2

    def test_shared_snapshot_reused_by_requests_and_run(self) -> None:
        request = ModelRequest(model_id="m-1", snapshot=self.snapshot)
        run = ExecutionRun(
            run_id="run-1",
            snapshot=self.snapshot,
            model_ids=("m-1", "m-2", "m-3"),
            status=ExecutionStatus.PENDING,
        )
        self.assertIs(request.snapshot, run.snapshot)
        self.assertEqual(len(run.model_ids), 3)

    def test_verification_status_has_three_outcomes(self) -> None:
        self.assertEqual(
            {status.value for status in VerificationStatus},
            {"verified", "failed", "inconclusive"},
        )
        result = VerificationResult(
            model_id="m-1", run_id="run-1", status=VerificationStatus.INCONCLUSIVE, evidence=None
        )
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)

    def test_individual_model_status_distinct_from_run_status(self) -> None:
        self.assertIn(ModelStatus.FAILED.value, {status.value for status in ModelStatus})
        self.assertEqual(ExecutionStatus.PARTIAL.value, "partial")


if __name__ == "__main__":
    unittest.main()
