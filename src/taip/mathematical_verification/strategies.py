
"""Verification strategies for a restricted linear equation domain."""

import re
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation

from taip.common.contracts import (
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
    VerificationStatus,
)


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
ANSWER_PATTERN = re.compile(
    rf"^x\s*=\s*({NUMBER})$"
)
CONSTRAINT_PATTERN = re.compile(
    rf"^x\s*(<=|>=|==|=|<|>)\s*({NUMBER})$"
)
EQUATION_PATTERN = re.compile(
    rf"^x\s*([+-])\s*({NUMBER.lstrip('[+-]?')})\s*=\s*({NUMBER})$"
)
SIMPLE_EQUATION_PATTERN = re.compile(
    rf"^x\s*=\s*({NUMBER})$"
)


def parse_answer(response: ModelResponse) -> Decimal | None:
    """Parse a single numeric assignment to x."""
    if response.status != ModelStatus.COMPLETED:
        return None

    if response.output is None:
        return None

    match = ANSWER_PATTERN.fullmatch(response.output.strip())

    if match is None:
        return None

    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def result_for(
    response: ModelResponse,
    status: VerificationStatus,
    evidence: str,
) -> VerificationResult:
    """Build a verification result preserving model and run IDs."""
    return VerificationResult(
        model_id=response.model_id,
        run_id=response.run_id,
        status=status,
        evidence=evidence,
    )


class VerificationStrategy(ABC):
    """Common interface for verification strategies."""

    @abstractmethod
    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        """Verify a response against a confirmed snapshot."""
        ...


class ConstraintSatisfactionStrategy(VerificationStrategy):
    """Check supported numeric constraints on x."""

    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        value = parse_answer(response)

        if value is None:
            return result_for(
                response,
                VerificationStatus.INCONCLUSIVE,
                "No supported numeric assignment was available.",
            )

        if not snapshot.constraints:
            return result_for(
                response,
                VerificationStatus.VERIFIED,
                "No constraints were defined.",
            )

        parsed_constraints = []

        for constraint in snapshot.constraints:
            match = CONSTRAINT_PATTERN.fullmatch(
                constraint.expression.strip()
            )

            if match is None:
                return result_for(
                    response,
                    VerificationStatus.INCONCLUSIVE,
                    f"Unsupported constraint: {constraint.expression}",
                )

            operator, number = match.groups()
            parsed_constraints.append(
                (constraint.constraint_id, operator, Decimal(number))
            )

        operations = {
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
            "=": lambda a, b: a == b,
            "==": lambda a, b: a == b,
        }

        for constraint_id, operator, bound in parsed_constraints:
            if not operations[operator](value, bound):
                return result_for(
                    response,
                    VerificationStatus.FAILED,
                    f"Constraint {constraint_id} violated.",
                )

        return result_for(
            response,
            VerificationStatus.VERIFIED,
            "All supported constraints are satisfied.",
        )


class MathematicalConsistencyStrategy(VerificationStrategy):
    """Check supported single-variable linear equations."""

    def verify(
        self, snapshot: ProblemSnapshot, response: ModelResponse
    ) -> VerificationResult:
        value = parse_answer(response)

        if value is None:
            return result_for(
                response,
                VerificationStatus.INCONCLUSIVE,
                "Cannot parse a completed numeric model response.",
            )

        prompt = snapshot.original_prompt.strip()

        if prompt.lower().startswith("solve "):
            equation = prompt[6:].strip()
        else:
            equation = prompt

        simple = SIMPLE_EQUATION_PATTERN.fullmatch(equation)

        if simple is not None:
            expected = Decimal(simple.group(1))
        else:
            match = EQUATION_PATTERN.fullmatch(equation)

            if match is None:
                return result_for(
                    response,
                    VerificationStatus.INCONCLUSIVE,
                    "Unsupported equation format.",
                )

            operator, number, right = match.groups()
            offset = Decimal(number)
            right_side = Decimal(right)

            if operator == "+":
                expected = right_side - offset
            else:
                expected = right_side + offset

        if value == expected:
            return result_for(
                response,
                VerificationStatus.VERIFIED,
                f"Candidate x = {value} satisfies the equation.",
            )

        return result_for(
            response,
            VerificationStatus.FAILED,
            f"Candidate x = {value} does not satisfy the equation.",
        )