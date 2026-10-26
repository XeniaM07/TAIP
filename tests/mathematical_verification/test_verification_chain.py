"""Structural tests for the verification chain."""

import inspect
import unittest
from typing import get_type_hints

from taip.common.contracts import (
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
    VerificationStatus,
)
from taip.mathematical_verification.verification_chain import (
    ConstraintVerificationHandler,
    InputValidationHandler,
    MathematicalVerificationHandler,
    VerificationHandler,
)


class VerificationChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = ProblemSnapshot(
            problem_id="p-1",
            version=1,
            original_prompt="Solve x = 2",
            objective="Find x",
            constraints=(),
        )
        self.response = ModelResponse(
            model_id="m-1",
            run_id="run-1",
            output="x = 2",
            status=ModelStatus.COMPLETED,
        )

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

    def test_handlers_can_be_connected_and_return_inconclusive_fallback(self) -> None:
        first = InputValidationHandler()
        second = ConstraintVerificationHandler()
        third = MathematicalVerificationHandler()
        self.assertIs(first.set_next(second), second)
        self.assertIs(second.set_next(third), third)

        result = first.handle(self.snapshot, self.response)

        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)
        self.assertEqual(result.model_id, self.response.model_id)
        self.assertEqual(result.run_id, self.response.run_id)
        self.assertIn("without a conclusive result", result.evidence)

    def test_input_validation_does_not_invent_verification_success(self) -> None:
        response = ModelResponse(
            model_id="m-1",
            run_id="run-1",
            output=None,
            status=ModelStatus.FAILED,
        )
        result = InputValidationHandler().handle(self.snapshot, response)
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)


if __name__ == "__main__":
    unittest.main()