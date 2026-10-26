"""Extensible chain for staged model-response verification."""

from __future__ import annotations

from abc import ABC, abstractmethod

from taip.common.contracts import (
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
    VerificationStatus,
)


class VerificationHandler(ABC):
    """Base handler that links verification steps in a chain."""

    def __init__(self) -> None:
        self._next_handler: VerificationHandler | None = None

    def set_next(self, handler: VerificationHandler) -> VerificationHandler:
        """Link the next handler and return it for fluent chain assembly."""
        self._next_handler = handler
        return handler

    @abstractmethod
    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Process the current stage or delegate to the next handler."""
        ...

    def _continue(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Delegate, or finish without claiming a conclusive verification."""
        if self._next_handler is not None:
            return self._next_handler.handle(snapshot, response)
        return VerificationResult(
            model_id=response.model_id,
            run_id=response.run_id,
            status=VerificationStatus.INCONCLUSIVE,
            evidence="Verification processing ended without a conclusive result.",
        )


class InputValidationHandler(VerificationHandler):
    """Reject structurally missing identifiers and route unusable responses."""

    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Validate required identifiers before delegating verification."""
        if not snapshot.problem_id.strip() or not response.model_id.strip() or not response.run_id.strip():
            return VerificationResult(
                model_id=response.model_id,
                run_id=response.run_id,
                status=VerificationStatus.FAILED,
                evidence="Problem, model, and run identifiers must be non-empty.",
            )
        if response.status is not ModelStatus.COMPLETED or not response.output or not response.output.strip():
            return VerificationResult(
                model_id=response.model_id,
                run_id=response.run_id,
                status=VerificationStatus.INCONCLUSIVE,
                evidence="A completed model response with non-empty output is required for verification.",
            )
        return self._continue(snapshot, response)


class ConstraintVerificationHandler(VerificationHandler):
    """Reserve a chain stage for constraint verification."""

    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Delegate while constraint evaluation remains unimplemented."""
        return self._continue(snapshot, response)


class MathematicalVerificationHandler(VerificationHandler):
    """Reserve a chain stage for mathematical consistency verification."""

    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Delegate while mathematical checks remain unimplemented."""
        return self._continue(snapshot, response)