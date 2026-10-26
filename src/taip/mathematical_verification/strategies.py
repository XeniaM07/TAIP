"""Pluggable strategies for verifying model responses."""

from abc import ABC, abstractmethod

from taip.common.contracts import (
    ModelResponse,
    ProblemSnapshot,
    VerificationResult,
    VerificationStatus,
)


class VerificationStrategy(ABC):
    """Common interface for verification strategies."""

    @abstractmethod
    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Verify a model response against a problem snapshot."""
        ...

    def _inconclusive_result(self, response: ModelResponse, evidence: str) -> VerificationResult:
        """Build a shared-contract result without claiming verification."""
        return VerificationResult(
            model_id=response.model_id,
            run_id=response.run_id,
            status=VerificationStatus.INCONCLUSIVE,
            evidence=evidence,
        )


class ConstraintSatisfactionStrategy(VerificationStrategy):
    """Declare constraint checking; evaluation is not implemented yet."""

    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Return an inconclusive result until constraint evaluation exists."""
        return self._inconclusive_result(
            response,
            "Constraint satisfaction is not evaluated in this structural implementation.",
        )


class MathematicalConsistencyStrategy(VerificationStrategy):
    """Declare mathematical consistency checking without claiming a proof."""

    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Return an inconclusive result until consistency checks exist."""
        return self._inconclusive_result(
            response,
            "Mathematical consistency is not evaluated in this structural implementation.",
        )