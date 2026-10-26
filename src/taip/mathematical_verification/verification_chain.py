"""Extensible chain for staged model-response verification."""

from __future__ import annotations

from abc import ABC, abstractmethod

from taip.common.contracts import (
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
)
from taip.mathematical_verification.strategies import (
    ConstraintSatisfactionStrategy,
    MathematicalConsistencyStrategy,
)


class VerificationHandler(ABC):
    """Base handler that links verification steps in a chain."""

    next_handler: VerificationHandler | None

    def __init__(self) -> None:
        ...

    def set_next(self, handler: VerificationHandler) -> VerificationHandler:
        ...

    @abstractmethod
    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Process the current stage or delegate to the next handler."""
        ...

    def _continue(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        ...


class InputValidationHandler(VerificationHandler):
    """Declare the initial validation stage of the verification chain."""

    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        ...


class ConstraintVerificationHandler(VerificationHandler):
    """Chain stage composed with the constraint-satisfaction strategy."""

    strategy: ConstraintSatisfactionStrategy

    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        ...


class MathematicalVerificationHandler(VerificationHandler):
    """Chain stage composed with the mathematical-consistency strategy."""

    strategy: MathematicalConsistencyStrategy

    def handle(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        ...