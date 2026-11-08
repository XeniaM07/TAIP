"""Stable, provider-independent school timetabling domain contracts (v1).

This module intentionally does not depend on AI providers, persistence or UI.
The existing generic contracts.py remains untouched for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias


class Weekday(str, Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"


class ConstraintSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class ConstraintType(str, Enum):
    TEACHER_UNAVAILABLE = "teacher_unavailable"
    ROOM_UNAVAILABLE = "room_unavailable"
    CLASS_UNAVAILABLE = "class_unavailable"
    ROOM_FEATURE_REQUIRED = "room_feature_required"
    MAX_CLASS_PERIODS_PER_DAY = "max_class_periods_per_day"
    TEACHER_PREFERRED_SLOTS = "teacher_preferred_slots"
    MINIMIZE_TEACHER_GAPS = "minimize_teacher_gaps"
    MINIMIZE_CLASS_GAPS = "minimize_class_gaps"
    BALANCE_CLASS_DAILY_LOAD = "balance_class_daily_load"


class SessionStatus(str, Enum):
    NEW = "new"
    EXTRACTING = "extracting"
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    SOLVING = "solving"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SchoolClass:
    class_id: str
    name: str
    student_count: int | None = None


@dataclass(frozen=True, slots=True)
class Teacher:
    teacher_id: str
    name: str
    subject_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Subject:
    subject_id: str
    name: str


@dataclass(frozen=True, slots=True)
class Room:
    room_id: str
    name: str
    capacity: int | None = None
    features: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TimeSlot:
    slot_id: str
    day: Weekday
    period: int
    start_time: str | None = None  # HH:MM, local school time
    end_time: str | None = None


@dataclass(frozen=True, slots=True)
class TeachingRequirement:
    requirement_id: str
    class_id: str
    subject_id: str
    teacher_id: str
    sessions_per_week: int


@dataclass(frozen=True, slots=True)
class TeacherUnavailable:
    teacher_id: str
    slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomUnavailable:
    room_id: str
    slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClassUnavailable:
    class_id: str
    slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomFeatureRequired:
    requirement_id: str
    feature: str


@dataclass(frozen=True, slots=True)
class MaxClassPeriodsPerDay:
    class_id: str
    maximum: int


@dataclass(frozen=True, slots=True)
class TeacherPreferredSlots:
    teacher_id: str
    slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MinimizeTeacherGaps:
    teacher_id: str | None = None  # None means all teachers


@dataclass(frozen=True, slots=True)
class MinimizeClassGaps:
    class_id: str | None = None  # None means all classes


@dataclass(frozen=True, slots=True)
class BalanceClassDailyLoad:
    class_id: str | None = None


ConstraintParameters: TypeAlias = (
    TeacherUnavailable
    | RoomUnavailable
    | ClassUnavailable
    | RoomFeatureRequired
    | MaxClassPeriodsPerDay
    | TeacherPreferredSlots
    | MinimizeTeacherGaps
    | MinimizeClassGaps
    | BalanceClassDailyLoad
)


@dataclass(frozen=True, slots=True)
class TimetableConstraint:
    constraint_id: str
    type: ConstraintType
    severity: ConstraintSeverity
    parameters: ConstraintParameters
    weight: int = 1  # relevant only for SOFT constraints
    source_text: str | None = None


@dataclass(frozen=True, slots=True)
class TimetableData:
    classes: tuple[SchoolClass, ...] = ()
    teachers: tuple[Teacher, ...] = ()
    subjects: tuple[Subject, ...] = ()
    rooms: tuple[Room, ...] = ()
    time_slots: tuple[TimeSlot, ...] = ()
    requirements: tuple[TeachingRequirement, ...] = ()
    constraints: tuple[TimetableConstraint, ...] = ()


@dataclass(frozen=True, slots=True)
class OriginalProblem:
    problem_id: str
    prompt: str


@dataclass(frozen=True, slots=True)
class MissingInformation:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionProposal:
    proposal_id: str
    problem_id: str
    model_id: str
    data: TimetableData
    missing_information: tuple[MissingInformation, ...] = ()
    unsupported_requirements: tuple[str, ...] = ()
    raw_response: str | None = None


@dataclass(frozen=True, slots=True)
class ProblemDraft:
    draft_id: str
    proposal_id: str
    problem_id: str
    model_id: str
    revision: int
    data: TimetableData


@dataclass(frozen=True, slots=True)
class TimetableProblemSnapshot:
    snapshot_id: str
    draft_id: str
    problem_id: str
    model_id: str
    version: int
    original_prompt: str
    data: TimetableData


@dataclass(frozen=True, slots=True)
class LessonAssignment:
    requirement_id: str
    occurrence: int  # zero-based occurrence within a teaching requirement
    slot_id: str
    room_id: str


@dataclass(frozen=True, slots=True)
class TimetableSolution:
    solution_id: str
    snapshot_id: str
    model_id: str
    assignments: tuple[LessonAssignment, ...]


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    code: str
    message: str
    constraint_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    slot_ids: tuple[str, ...] = ()
    severity: ConstraintSeverity = ConstraintSeverity.HARD
    lessons: tuple[LessonAssignment, ...] = ()


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class TimetableVerificationResult:
    solution_id: str
    snapshot_id: str
    status: VerificationStatus
    violations: tuple[ConstraintViolation, ...] = ()
    soft_penalty: int | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelRunResult:
    model_id: str
    proposal_id: str | None = None
    snapshot_id: str | None = None
    solution_id: str | None = None
    verification_status: VerificationStatus | None = None
    extraction_duration_ms: int | None = None
    solving_duration_ms: int | None = None
    edit_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ComparisonRun:
    comparison_id: str
    problem_id: str
    model_runs: tuple[ModelRunResult, ...]
