"""Structural tests for mathematical verification strategies."""

import inspect
import unittest
from typing import get_type_hints

from taip.common.contracts import ModelResponse, ProblemSnapshot, VerificationResult
from taip.mathematical_verification.strategies import (
    ConstraintSatisfactionStrategy,
    MathematicalConsistencyStrategy,
    VerificationStrategy,
)


class VerificationStrategyTests(unittest.TestCase):
    def test_strategy_interface_is_abstract(self) -> None:
        self.assertTrue(inspect.isabstract(VerificationStrategy))

    def test_concrete_strategies_implement_the_interface(self) -> None:
        for strategy_type in (
            ConstraintSatisfactionStrategy,
            MathematicalConsistencyStrategy,
        ):
            with self.subTest(strategy=strategy_type.__name__):
                self.assertTrue(issubclass(strategy_type, VerificationStrategy))
                self.assertFalse(inspect.isabstract(strategy_type))

    def test_verify_signature_uses_shared_contracts(self) -> None:
        expected_hints = {
            "snapshot": ProblemSnapshot,
            "response": ModelResponse,
            "return": VerificationResult,
        }
        self.assertEqual(get_type_hints(VerificationStrategy.verify), expected_hints)
        for strategy_type in (
            ConstraintSatisfactionStrategy,
            MathematicalConsistencyStrategy,
        ):
            with self.subTest(strategy=strategy_type.__name__):
                self.assertEqual(get_type_hints(strategy_type.verify), expected_hints)


if __name__ == "__main__":
    unittest.main()