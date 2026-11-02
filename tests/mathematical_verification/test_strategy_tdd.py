
"""TDD tests for mathematical verification strategies."""

import unittest

from taip.common.contracts import (
    Constraint,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationStatus,
)
from taip.mathematical_verification.strategies import (
    ConstraintSatisfactionStrategy,
    MathematicalConsistencyStrategy,
    VerificationStrategy,
)


class VerificationStrategyTDDTests(unittest.TestCase):

    def setUp(self) -> None:
        self.snapshot = ProblemSnapshot(
            problem_id="problem-1",
            version=1,
            original_prompt="Solve x + 2 = 5",
            objective="Find x",
            constraints=(
                Constraint(
                    constraint_id="c1",
                    expression="x > 0",
                ),
            ),
        )

        self.response = ModelResponse(
            model_id="openai-test",
            run_id="run-1",
            output="x = 3",
            status=ModelStatus.COMPLETED,
        )

    def test_constraint_strategy_implements_common_interface(self):
        strategy = ConstraintSatisfactionStrategy()

        self.assertIsInstance(strategy, VerificationStrategy)

    def test_mathematical_strategy_implements_common_interface(self):
        strategy = MathematicalConsistencyStrategy()

        self.assertIsInstance(strategy, VerificationStrategy)

    def test_constraint_strategy_returns_inconclusive_without_output(self):
        strategy = ConstraintSatisfactionStrategy()

        response = ModelResponse(
            model_id="openai-test",
            run_id="run-1",
            output=None,
            status=ModelStatus.COMPLETED,
        )

        result = strategy.verify(self.snapshot, response)

        self.assertEqual(
            result.status,
            VerificationStatus.INCONCLUSIVE,
        )

    def test_mathematical_strategy_returns_inconclusive_without_output(self):
        strategy = MathematicalConsistencyStrategy()

        response = ModelResponse(
            model_id="openai-test",
            run_id="run-1",
            output=None,
            status=ModelStatus.COMPLETED,
        )

        result = strategy.verify(self.snapshot, response)

        self.assertEqual(
            result.status,
            VerificationStatus.INCONCLUSIVE,
        )

    def test_constraint_result_preserves_response_identifiers(self):
        strategy = ConstraintSatisfactionStrategy()

        response = ModelResponse(
            model_id="gemini-test",
            run_id="run-42",
            output=None,
            status=ModelStatus.FAILED,
        )

        result = strategy.verify(self.snapshot, response)

        self.assertEqual(result.model_id, response.model_id)
        self.assertEqual(result.run_id, response.run_id)

    def test_mathematical_result_preserves_response_identifiers(self):
        strategy = MathematicalConsistencyStrategy()

        response = ModelResponse(
            model_id="gemini-test",
            run_id="run-42",
            output=None,
            status=ModelStatus.FAILED,
        )

        result = strategy.verify(self.snapshot, response)

        self.assertEqual(result.model_id, response.model_id)
        self.assertEqual(result.run_id, response.run_id)

    def test_strategies_are_independent_objects(self):
        constraint_strategy = ConstraintSatisfactionStrategy()
        mathematical_strategy = MathematicalConsistencyStrategy()

        self.assertIsNot(
            constraint_strategy,
            mathematical_strategy,
        )


if __name__ == "__main__":
    unittest.main()
