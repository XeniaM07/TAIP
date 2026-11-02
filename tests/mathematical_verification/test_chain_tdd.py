"""Functional TDD tests for Chain of Responsibility."""

import unittest

from taip.common.contracts import (
    Constraint,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationStatus,
)
from taip.mathematical_verification.verification_chain import (
    InputValidationHandler,
    ConstraintVerificationHandler,
    MathematicalVerificationHandler,
)


class VerificationChainTDDTests(unittest.TestCase):

    def setUp(self):
        self.snapshot = ProblemSnapshot(
            problem_id="problem-1",
            version=1,
            original_prompt="Solve x + 2 = 5",
            objective="Find x",
            constraints=(Constraint("c1", "x >= 0"),),
        )

        self.input_handler = InputValidationHandler()
        self.constraint_handler = ConstraintVerificationHandler()
        self.math_handler = MathematicalVerificationHandler()

        self.input_handler.set_next(self.constraint_handler)
        self.constraint_handler.set_next(self.math_handler)

    def response(
        self, output, status=ModelStatus.COMPLETED
    ):
        return ModelResponse(
            model_id="model-test",
            run_id="run-1",
            output=output,
            status=status,
        )

    def test_valid_solution_passes_all_stages(self):
        result = self.input_handler.handle(
            self.snapshot, self.response("x = 3")
        )

        self.assertEqual(
            result.status, VerificationStatus.VERIFIED
        )
        self.assertIsNotNone(result.evidence)

    def test_incorrect_solution_fails_math_stage(self):
        result = self.input_handler.handle(
            self.snapshot, self.response("x = 4")
        )

        self.assertEqual(
            result.status, VerificationStatus.FAILED
        )

    def test_constraint_violation_stops_chain(self):
        result = self.input_handler.handle(
            self.snapshot, self.response("x = -1")
        )

        self.assertEqual(
            result.status, VerificationStatus.FAILED
        )
        self.assertIn("Constraint", result.evidence)

    def test_missing_response_is_inconclusive(self):
        result = self.input_handler.handle(
            self.snapshot,
            self.response(None, ModelStatus.FAILED),
        )

        self.assertEqual(
            result.status, VerificationStatus.INCONCLUSIVE
        )

    def test_invalid_answer_format_is_inconclusive(self):
        result = self.input_handler.handle(
            self.snapshot, self.response("Unknown answer")
        )

        self.assertEqual(
            result.status, VerificationStatus.INCONCLUSIVE
        )

    def test_chain_preserves_identifiers(self):
        result = self.input_handler.handle(
            self.snapshot, self.response("x = 3")
        )

        self.assertEqual(result.model_id, "model-test")
        self.assertEqual(result.run_id, "run-1")

    def test_set_next_returns_handler(self):
        new_handler = MathematicalVerificationHandler()

        returned = self.constraint_handler.set_next(
            new_handler
        )

        self.assertIs(returned, new_handler)
        self.assertIs(
            self.constraint_handler.next_handler,
            new_handler,
        )

    def test_unlinked_input_handler_returns_verified(self):
        handler = InputValidationHandler()

        result = handler.handle(
            self.snapshot, self.response("x = 3")
        )

        self.assertEqual(
            result.status, VerificationStatus.VERIFIED
        )


if __name__ == "__main__":
    unittest.main()