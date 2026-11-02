"""Chain of Responsibility for staged mathematical verification."""

from __future__ import annotations

from abc import ABC, abstractmethod

from taip.common.contracts import (
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
    VerificationStatus,
)
from taip.mathematical_verification.strategies import (
    ConstraintSatisfactionStrategy,
    MathematicalConsistencyStrategy,
    parse_answer,
    result_for,
)


class VerificationHandler(ABC):
    """Base handler linking verification stages."""

    next_handler: VerificationHandler | None

    def __init__(self) -> None:
        self.next_handler = None

    def set_next(
        self, handler: VerificationHandler
    ) -> VerificationHandler:
        """Set the next handler and return it."""
        self.next_handler = handler
        return handler

    @abstractmethod
    def handle(
        self,
        snapshot: ProblemSnapshot,
        response: ModelResponse,
    ) -> VerificationResult:
        """Execute the current verification stage."""
        ...

    def _continue(
        self,
        snapshot: ProblemSnapshot,
        response: ModelResponse,
    ) -> VerificationResult:
        """Pass control to the next stage if available."""
        if self.next_handler is not None:
            return self.next_handler.handle(
                snapshot, response
            )

        return result_for(
            response,
            VerificationStatus.VERIFIED,
            "Current verification stage completed successfully.",
        )


class InputValidationHandler(VerificationHandler):
    """Validate that a model response can be processed."""

    def handle(
        self,
        snapshot: ProblemSnapshot,
        response: ModelResponse,
    ) -> VerificationResult:
        if response.status != ModelStatus.COMPLETED:
            return result_for(
                response,
                VerificationStatus.INCONCLUSIVE,
                "Model execution did not complete.",
            )

        if parse_answer(response) is None:
            return result_for(
                response,
                VerificationStatus.INCONCLUSIVE,
                "Response is not a supported numeric assignment.",
            )

        return self._continue(snapshot, response)


class ConstraintVerificationHandler(VerificationHandler):
    """Verify constraints using the configured strategy."""

    strategy: ConstraintSatisfactionStrategy

    def __init__(self) -> None:
        super().__init__()
        self.strategy = ConstraintSatisfactionStrategy()

    def handle(
        self,
        snapshot: ProblemSnapshot,
        response: ModelResponse,
    ) -> VerificationResult:
        result = self.strategy.verify(snapshot, response)

        if result.status != VerificationStatus.VERIFIED:
            return result

        if self.next_handler is None:
            return result

        return self._continue(snapshot, response)


class MathematicalVerificationHandler(VerificationHandler):
    """Verify mathematical consistency using a strategy."""

    strategy: MathematicalConsistencyStrategy

    def __init__(self) -> None:
        super().__init__()
        self.strategy = MathematicalConsistencyStrategy()

    def handle(
        self,
        snapshot: ProblemSnapshot,
        response: ModelResponse,
    ) -> VerificationResult:
        result = self.strategy.verify(snapshot, response)

        if result.status != VerificationStatus.VERIFIED:
            return result

        if self.next_handler is None:
            return result

        return self._continue(snapshot, response)