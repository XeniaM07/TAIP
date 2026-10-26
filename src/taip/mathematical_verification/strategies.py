"""Pluggable strategies for verifying model responses."""

from abc import ABC, abstractmethod

from taip.common.contracts import (
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
)


class VerificationStrategy(ABC):
    """Common interface for verification strategies."""

    @abstractmethod
    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Verify a model response against a problem snapshot."""
        ...


class ConstraintSatisfactionStrategy(VerificationStrategy):
    """Declare the strategy for checking problem constraints."""

    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        ...


class MathematicalConsistencyStrategy(VerificationStrategy):
    """Declare the strategy for checking mathematical consistency."""

    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        ...