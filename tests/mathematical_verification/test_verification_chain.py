"""Structural tests for the verification chain."""

import inspect
import unittest
from typing import get_type_hints

from taip.common.contracts import (
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
)
from taip.mathematical_verification.verification_chain import (
    ConstraintVerificationHandler,
    InputValidationHandler,
    MathematicalVerificationHandler,
    VerificationHandler,
)
from taip.mathematical_verification.strategies import (
    ConstraintSatisfactionStrategy,
    MathematicalConsistencyStrategy,
)


class VerificationChainTests(unittest.TestCase):
    def test_base_is_abstract_and_concrete_handlers_inherit(self) -> None:
        self.assertTrue(inspect.isabstract(VerificationHandler))
        for handler_type in (
            InputValidationHandler,
            ConstraintVerificationHandler,
            MathematicalVerificationHandler,
        ):
            with self.subTest(handler=handler_type.__name__):
                self.assertTrue(issubclass(handler_type, VerificationHandler))
                self.assertFalse(inspect.isabstract(handler_type))

    def test_shared_handler_methods_have_contract_types(self) -> None:
        expected_handle_hints = {
            "snapshot": ProblemSnapshot,
            "response": ModelResponse,
            "return": VerificationResult,
        }
        self.assertIn("set_next", VerificationHandler.__dict__)
        self.assertIn("handle", VerificationHandler.__dict__)
        self.assertEqual(get_type_hints(VerificationHandler.handle), expected_handle_hints)
        self.assertEqual(
            get_type_hints(VerificationHandler.set_next),
            {"handler": VerificationHandler, "return": VerificationHandler},
        )

    def test_chain_declares_link_and_strategy_relationships(self) -> None:
        self.assertEqual(
            get_type_hints(VerificationHandler),
            {"next_handler": VerificationHandler | None},
        )
        self.assertEqual(
            get_type_hints(ConstraintVerificationHandler),
            {
                "next_handler": VerificationHandler | None,
                "strategy": ConstraintSatisfactionStrategy,
            },
        )
        self.assertEqual(
            get_type_hints(MathematicalVerificationHandler),
            {
                "next_handler": VerificationHandler | None,
                "strategy": MathematicalConsistencyStrategy,
            },
        )

if __name__ == "__main__":
    unittest.main()