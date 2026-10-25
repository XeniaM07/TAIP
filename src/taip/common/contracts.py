"""Shared structural contracts for the TAIP modules (Lab 04)."""

from dataclasses import dataclass
from enum import Enum


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class ModelStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Constraint:
    constraint_id: str
    expression: str


@dataclass(frozen=True, slots=True)
class ProblemSnapshot:
    problem_id: str
    version: int
    original_prompt: str
    objective: str
    constraints: tuple[Constraint, ...]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model_id: str
    snapshot: ProblemSnapshot


@dataclass(frozen=True, slots=True)
class ModelResponse:
    model_id: str
    run_id: str
    output: str | None
    status: ModelStatus


@dataclass(frozen=True, slots=True)
class VerificationResult:
    model_id: str
    run_id: str
    status: VerificationStatus
    evidence: str | None


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    run_id: str
    snapshot: ProblemSnapshot
    model_ids: tuple[str, str, str]
    status: ExecutionStatus
