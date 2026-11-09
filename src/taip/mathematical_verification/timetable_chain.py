"""Chain of Responsibility stages for independent timetable verification."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import asdict, dataclass, field
import json
from typing import Callable, ClassVar

from taip.common.timetable_contracts import (
    BalanceClassDailyLoad,
    ClassUnavailable,
    ConstraintSeverity,
    ConstraintType,
    ConstraintViolation,
    LessonAssignment,
    MaxClassPeriodsPerDay,
    MinimizeClassGaps,
    MinimizeTeacherGaps,
    Room,
    RoomFeatureRequired,
    RoomUnavailable,
    SchoolClass,
    Teacher,
    TeacherPreferredSlots,
    TeacherUnavailable,
    TeachingRequirement,
    TimeSlot,
    TimetableConstraint,
    TimetableProblemSnapshot,
    TimetableSolution,
    VerificationStatus,
)
from taip.common.timetable_integrity import (
    MAX_TOTAL_SESSIONS,
    iter_timetable_integrity_issues,
)


class _VerificationBudgetExceeded(Exception):
    """Internal control flow: stop checks once their bounded report is full."""


@dataclass(slots=True)
class VerificationContext:
    """Bound report construction before serialization, preserving uncertainty."""

    MAX_FINDINGS: ClassVar[int] = 500
    MAX_ATTACHED_LESSONS: ClassVar[int] = 2000
    MAX_DETAIL_BYTES: ClassVar[int] = 512 * 1024
    MAX_LESSONS_PER_FINDING: ClassVar[int] = 100
    _DETAIL_RESERVE: ClassVar[int] = 4096
    _TRUNCATION_NOTE: ClassVar[str] = (
        "Verification details truncated at the resource budget; remaining "
        "checks and the soft penalty may be incomplete."
    )

    snapshot: TimetableProblemSnapshot
    solution: TimetableSolution
    violations: list[ConstraintViolation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    soft_penalty: int = 0
    inconclusive: bool = False
    stop_verification: bool = False
    _hard_failure: bool = field(default=False, init=False)
    _attached_lessons: int = field(default=0, init=False)
    _detail_bytes: int = field(default=0, init=False)
    _finding_bytes: list[int] = field(default_factory=list, init=False)

    def _mark_truncated(self) -> None:
        self.inconclusive = True
        if self._TRUNCATION_NOTE not in self.notes:
            self.notes.append(self._TRUNCATION_NOTE)

    def _bounded_text(self, value: str, maximum: int = 4096) -> str:
        if len(value) <= maximum:
            return value
        self._mark_truncated()
        return value[:maximum] + " [truncated]"

    def _bounded_ids(self, values: tuple[str, ...]) -> tuple[str, ...]:
        unique = tuple(dict.fromkeys(values))
        bounded = tuple(value for value in unique[:100] if len(value) <= 4096)
        if len(bounded) != len(unique):
            self._mark_truncated()
        return bounded

    def _stop_at_budget(self, finding: ConstraintViolation | None = None) -> None:
        self._mark_truncated()
        self.stop_verification = True
        # Retain the actual hard reason even if earlier soft details filled the
        # budget. Never infer feasibility from only the retained sample.
        if (
            finding is not None
            and finding.severity == ConstraintSeverity.HARD
            and not any(
                item.severity == ConstraintSeverity.HARD for item in self.violations
            )
        ):
            compact = ConstraintViolation(
                code=finding.code,
                message=finding.message[:256],
                severity=ConstraintSeverity.HARD,
            )
            size = len(json.dumps(asdict(compact)))
            while self.violations and (
                len(self.violations) >= self.MAX_FINDINGS
                or self._detail_bytes + size > self.MAX_DETAIL_BYTES - self._DETAIL_RESERVE
            ):
                removed = self.violations.pop()
                self._detail_bytes -= self._finding_bytes.pop()
                self._attached_lessons -= len(removed.lessons)
            self.violations.append(compact)
            self._finding_bytes.append(size)
            self._detail_bytes += size
        raise _VerificationBudgetExceeded

    def add_note(self, message: str) -> None:
        """Include notes in the same byte/entry budget as violation details."""
        message = self._bounded_text(message)
        if message in self.notes:
            return
        size = len(json.dumps(message))
        if (
            len(self.notes) >= self.MAX_FINDINGS
            or self._detail_bytes + size > self.MAX_DETAIL_BYTES - self._DETAIL_RESERVE
        ):
            self._stop_at_budget()
        self.notes.append(message)
        self._detail_bytes += size

    def add_finding(
        self,
        code: str,
        message: str,
        *,
        constraint: TimetableConstraint | None = None,
        lessons: tuple[LessonAssignment, ...] = (),
        requirement_ids: tuple[str, ...] | None = None,
        severity: ConstraintSeverity = ConstraintSeverity.HARD,
    ) -> None:
        self._hard_failure |= severity == ConstraintSeverity.HARD
        if len(self.violations) >= self.MAX_FINDINGS:
            self._stop_at_budget(
                ConstraintViolation(
                    code=code, message=self._bounded_text(message), severity=severity,
                )
            )
        remaining = self.MAX_ATTACHED_LESSONS - self._attached_lessons
        if lessons and remaining <= 0:
            self._stop_at_budget(
                ConstraintViolation(
                    code=code, message=self._bounded_text(message), severity=severity,
                )
            )
        sampled: dict[LessonAssignment, None] = {}
        for lesson in lessons:
            # Malformed or oversized occurrences cannot safely be serialized.
            # Report their violation without copying that input payload.
            if (
                type(lesson.occurrence) is not int
                or not -MAX_TOTAL_SESSIONS <= lesson.occurrence <= MAX_TOTAL_SESSIONS
                or not all(
                    isinstance(value, str) and len(value) <= 4096
                    for value in (lesson.requirement_id, lesson.slot_id, lesson.room_id)
                )
            ):
                self._mark_truncated()
                continue
            if lesson in sampled:
                continue
            if len(sampled) >= min(self.MAX_LESSONS_PER_FINDING, remaining):
                self._mark_truncated()
                break
            sampled[lesson] = None
        sampled_lessons = tuple(sampled)
        constraint_id = constraint.constraint_id if constraint is not None else None
        if constraint_id is not None and len(constraint_id) > 4096:
            constraint_id = None
            self._mark_truncated()
        finding = ConstraintViolation(
            code=code,
            message=self._bounded_text(message),
            constraint_id=constraint_id,
            requirement_ids=self._bounded_ids(
                requirement_ids
                if requirement_ids is not None
                else tuple(lesson.requirement_id for lesson in sampled_lessons)
            ),
            slot_ids=self._bounded_ids(
                tuple(lesson.slot_id for lesson in sampled_lessons)
            ),
            severity=severity,
            lessons=sampled_lessons,
        )
        size = len(json.dumps(asdict(finding))) + 2
        if self._detail_bytes + size > self.MAX_DETAIL_BYTES - self._DETAIL_RESERVE:
            self._stop_at_budget(finding)
        self.violations.append(finding)
        self._finding_bytes.append(size)
        self._detail_bytes += size
        self._attached_lessons += len(sampled_lessons)

    def add_soft_finding(
        self,
        constraint: TimetableConstraint,
        code: str,
        message: str,
        lessons: tuple[LessonAssignment, ...],
        units: int,
    ) -> None:
        penalty = units * constraint.weight
        self.soft_penalty += penalty
        self.add_finding(
            code,
            f"{message} Penalty: {penalty}.",
            constraint=constraint,
            lessons=lessons,
            severity=ConstraintSeverity.SOFT,
        )

    @property
    def status(self) -> VerificationStatus:
        if self._hard_failure or any(
            violation.severity == ConstraintSeverity.HARD
            for violation in self.violations
        ):
            return VerificationStatus.FAILED
        if self.inconclusive:
            return VerificationStatus.INCONCLUSIVE
        return VerificationStatus.VERIFIED


class TimetableVerificationHandler(ABC):
    """Base handler that runs a verification stage and continues the chain."""

    def __init__(self) -> None:
        self.next_handler: TimetableVerificationHandler | None = None

    def set_next(
        self, handler: TimetableVerificationHandler
    ) -> TimetableVerificationHandler:
        self.next_handler = handler
        return handler

    def handle(self, context: VerificationContext) -> None:
        if context.stop_verification:
            return
        try:
            self._verify(context)
        except _VerificationBudgetExceeded:
            return
        if self.next_handler is not None and not context.stop_verification:
            self.next_handler.handle(context)

    @abstractmethod
    def _verify(self, context: VerificationContext) -> None:
        """Check this stage and append findings without stopping later checks."""
        ...


class AssignmentIntegrityHandler(TimetableVerificationHandler):
    """Check identities, references, required counts, and occurrences."""

    def _verify(self, context: VerificationContext) -> None:
        snapshot = context.snapshot
        solution = context.solution
        if solution.snapshot_id != snapshot.snapshot_id:
            context.add_finding(
                "solution.snapshot_mismatch",
                "The solution references a different snapshot.",
            )
        if solution.model_id != snapshot.model_id:
            context.add_finding(
                "solution.model_mismatch",
                "The solution model does not match the confirmed snapshot.",
            )

        data = snapshot.data
        for issue in iter_timetable_integrity_issues(data):
            context.stop_verification = True
            code = (
                "snapshot.invalid_sessions_per_week"
                if issue.code == "INVALID_SESSIONS"
                else f"snapshot.{issue.code.lower()}"
            )
            context.add_finding(code, f"{issue.path}: {issue.message}")
        if len(solution.assignments) > MAX_TOTAL_SESSIONS:
            context.stop_verification = True
            context.add_finding(
                "solution.lesson_resource_limit",
                "Solution assignments exceed the supported limit of 10,000 lessons.",
            )
        if context.stop_verification:
            return
        requirements: dict[str, TeachingRequirement] = {}
        slots: dict[str, TimeSlot] = {}
        rooms: dict[str, Room] = {}
        classes: dict[str, SchoolClass] = {}
        teachers: dict[str, Teacher] = {}
        subjects: dict[str, object] = {}

        for collection_name, collection, id_field, index in (
            ("requirement", data.requirements, "requirement_id", requirements),
            ("time_slot", data.time_slots, "slot_id", slots),
            ("room", data.rooms, "room_id", rooms),
            ("class", data.classes, "class_id", classes),
            ("teacher", data.teachers, "teacher_id", teachers),
            ("subject", data.subjects, "subject_id", subjects),
        ):
            for item in collection:
                item_id = getattr(item, id_field)
                if not isinstance(item_id, str) or not item_id.strip():
                    context.add_finding(
                        f"snapshot.invalid_{collection_name}_id",
                        f"Snapshot contains an invalid {collection_name} ID.",
                    )
                    continue
                if item_id in index:
                    context.add_finding(
                        f"snapshot.duplicate_{collection_name}_id",
                        f"Snapshot contains duplicate {collection_name} ID "
                        f"'{item_id}'.",
                    )
                else:
                    index[item_id] = item

        assignments_by_requirement: dict[str, list[LessonAssignment]] = defaultdict(list)
        occurrences: dict[tuple[str, int], list[LessonAssignment]] = defaultdict(list)
        for lesson in solution.assignments:
            requirement = requirements.get(lesson.requirement_id)
            if requirement is None:
                context.add_finding(
                    "assignment.unknown_requirement",
                    f"Lesson references unknown requirement "
                    f"'{lesson.requirement_id}'.",
                    lessons=(lesson,),
                )
                continue
            assignments_by_requirement[lesson.requirement_id].append(lesson)

            if lesson.slot_id not in slots:
                context.add_finding(
                    "assignment.unknown_slot",
                    f"Lesson references unknown time slot '{lesson.slot_id}'.",
                    lessons=(lesson,),
                )
            if lesson.room_id not in rooms:
                context.add_finding(
                    "assignment.unknown_room",
                    f"Lesson references unknown room '{lesson.room_id}'.",
                    lessons=(lesson,),
                )
            expected = requirement.sessions_per_week
            valid_expected = (
                isinstance(expected, int)
                and not isinstance(expected, bool)
                and expected > 0
            )
            valid_occurrence = (
                isinstance(lesson.occurrence, int)
                and not isinstance(lesson.occurrence, bool)
            )
            if not valid_occurrence or (
                valid_expected
                and (lesson.occurrence < 0 or lesson.occurrence >= expected)
            ):
                context.add_finding(
                    "assignment.occurrence_out_of_range",
                    "Occurrence is outside the required range "
                    f"for '{lesson.requirement_id}'.",
                    lessons=(lesson,),
                )
            elif valid_occurrence:
                occurrences[(lesson.requirement_id, lesson.occurrence)].append(lesson)

        for (requirement_id, occurrence), lessons in occurrences.items():
            if len(lessons) > 1:
                context.add_finding(
                    "assignment.duplicate_occurrence",
                    f"Requirement '{requirement_id}' occurrence {occurrence} "
                    "is assigned more than once.",
                    lessons=tuple(lessons),
                )

        for requirement_id, requirement in requirements.items():
            lessons = tuple(assignments_by_requirement.get(requirement_id, ()))
            expected = requirement.sessions_per_week
            if (
                not isinstance(expected, int)
                or isinstance(expected, bool)
                or expected < 1
            ):
                context.add_finding(
                    "snapshot.invalid_sessions_per_week",
                    f"Requirement '{requirement_id}' has an invalid weekly lesson "
                    "count.",
                    lessons=lessons,
                )
                continue
            if len(lessons) != expected:
                context.add_finding(
                    "requirement.lesson_count",
                    f"Requirement '{requirement_id}' needs exactly {expected} "
                    f"lessons; found {len(lessons)}.",
                    lessons=lessons,
                    requirement_ids=(requirement_id,),
                )
            actual_occurrences = {
                lesson.occurrence
                for lesson in lessons
                if isinstance(lesson.occurrence, int)
                and not isinstance(lesson.occurrence, bool)
            }
            for missing_occurrence in sorted(set(range(expected)) - actual_occurrences):
                context.add_finding(
                    "requirement.missing_occurrence",
                    f"Requirement '{requirement_id}' is missing occurrence "
                    f"{missing_occurrence}.",
                    requirement_ids=(requirement_id,),
                )
            if requirement.class_id not in classes:
                context.add_finding(
                    "snapshot.unknown_class",
                    f"Requirement '{requirement_id}' references unknown class "
                    f"'{requirement.class_id}'.",
                    lessons=lessons,
                )
            if requirement.teacher_id not in teachers:
                context.add_finding(
                    "snapshot.unknown_teacher",
                    f"Requirement '{requirement_id}' references unknown teacher "
                    f"'{requirement.teacher_id}'.",
                    lessons=lessons,
                )
            if requirement.subject_id not in subjects:
                context.add_finding(
                    "snapshot.unknown_subject",
                    f"Requirement '{requirement_id}' references unknown subject "
                    f"'{requirement.subject_id}'.",
                    lessons=lessons,
                )


class ResourceConflictHandler(TimetableVerificationHandler):
    """Detect simultaneous use of teachers, classes, and rooms."""

    def _verify(self, context: VerificationContext) -> None:
        data = context.snapshot.data
        requirements = {item.requirement_id: item for item in data.requirements}
        slot_ids = {slot.slot_id for slot in data.time_slots}
        resources: dict[tuple[str, str, str], list[LessonAssignment]] = defaultdict(list)
        for lesson in context.solution.assignments:
            requirement = requirements.get(lesson.requirement_id)
            if requirement is None or lesson.slot_id not in slot_ids:
                continue
            resources[("teacher", requirement.teacher_id, lesson.slot_id)].append(lesson)
            resources[("class", requirement.class_id, lesson.slot_id)].append(lesson)
            resources[("room", lesson.room_id, lesson.slot_id)].append(lesson)

        codes = {
            "teacher": "schedule.teacher_overlap",
            "class": "schedule.class_overlap",
            "room": "schedule.room_overlap",
        }
        for (resource_type, resource_id, slot_id), lessons in resources.items():
            if len(lessons) > 1:
                context.add_finding(
                    codes[resource_type],
                    f"{resource_type.title()} '{resource_id}' has "
                    f"{len(lessons)} lessons in slot '{slot_id}'.",
                    lessons=tuple(lessons),
                )


class ConstraintVerificationHandler(TimetableVerificationHandler):
    """Evaluate hard constraints, capacity, and supported soft objectives."""

    def _verify(self, context: VerificationContext) -> None:
        data = context.snapshot.data
        requirements = {item.requirement_id: item for item in data.requirements}
        classes = {item.class_id: item for item in data.classes}
        teachers = {item.teacher_id: item for item in data.teachers}
        rooms = {item.room_id: item for item in data.rooms}
        slots = {item.slot_id: item for item in data.time_slots}
        lessons_by_requirement: dict[str, list[LessonAssignment]] = defaultdict(list)
        for lesson in context.solution.assignments:
            lessons_by_requirement[lesson.requirement_id].append(lesson)

        self._check_room_capacity(context, requirements, classes, rooms)
        self._check_configured_constraints(
            context,
            requirements,
            classes,
            teachers,
            rooms,
            slots,
            lessons_by_requirement,
        )

    def _check_room_capacity(
        self,
        context: VerificationContext,
        requirements: dict[str, TeachingRequirement],
        classes: dict[str, SchoolClass],
        rooms: dict[str, Room],
    ) -> None:
        for lesson in context.solution.assignments:
            requirement = requirements.get(lesson.requirement_id)
            school_class = (
                classes.get(requirement.class_id) if requirement is not None else None
            )
            room = rooms.get(lesson.room_id)
            if school_class is None or room is None:
                continue
            student_count = school_class.student_count
            capacity = room.capacity
            if student_count is not None and (
                not isinstance(student_count, int)
                or isinstance(student_count, bool)
                or student_count < 0
            ):
                context.add_finding(
                    "snapshot.invalid_class_size",
                    f"Class '{school_class.class_id}' has an invalid student count.",
                    lessons=(lesson,),
                )
                continue
            if capacity is not None and (
                not isinstance(capacity, int)
                or isinstance(capacity, bool)
                or capacity < 0
            ):
                context.add_finding(
                    "snapshot.invalid_room_capacity",
                    f"Room '{room.room_id}' has an invalid capacity.",
                    lessons=(lesson,),
                )
                continue
            if student_count is None or capacity is None:
                context.inconclusive = True
                occurrence_label = (
                    str(lesson.occurrence)
                    if type(lesson.occurrence) is int
                    and -MAX_TOTAL_SESSIONS <= lesson.occurrence <= MAX_TOTAL_SESSIONS
                    else "invalid"
                )
                context.add_note(
                    f"Room capacity cannot be checked for lesson "
                    f"'{lesson.requirement_id}:{occurrence_label}' because class "
                    "size or room capacity is unknown."
                )
            elif student_count > capacity:
                context.add_finding(
                    "room.capacity",
                    f"Room '{room.room_id}' capacity {capacity} is below class "
                    f"size {student_count}.",
                    lessons=(lesson,),
                )

    def _check_configured_constraints(
        self,
        context: VerificationContext,
        requirements: dict[str, TeachingRequirement],
        classes: dict[str, SchoolClass],
        teachers: dict[str, Teacher],
        rooms: dict[str, Room],
        slots: dict[str, TimeSlot],
        lessons_by_requirement: dict[str, list[LessonAssignment]],
    ) -> None:
        # Check every hard rule before soft objectives can exhaust the report.
        for constraint in sorted(
            context.snapshot.data.constraints,
            key=lambda item: item.severity == ConstraintSeverity.SOFT,
        ):
            params = constraint.parameters
            if not self._parameters_match(constraint.type, params):
                context.add_finding(
                    "constraint.invalid_parameters",
                    f"Constraint '{constraint.constraint_id}' has parameters "
                    "incompatible with its type.",
                    constraint=constraint,
                )
                continue
            if not self._constraint_configuration_is_valid(constraint, params):
                context.add_finding(
                    "constraint.invalid_configuration",
                    f"Constraint '{constraint.constraint_id}' has an invalid "
                    "severity, weight, or parameter value.",
                    constraint=constraint,
                )
                continue

            if isinstance(params, TeacherUnavailable):
                if params.teacher_id not in teachers or any(
                    slot_id not in slots for slot_id in params.slot_ids
                ):
                    self._invalid_constraint_reference(context, constraint)
                    continue
                affected = self._matching_lessons(
                    lessons_by_requirement,
                    requirements,
                    lambda requirement: requirement.teacher_id == params.teacher_id,
                )
                self._apply_unavailability(
                    context, constraint, affected, params.slot_ids,
                    "constraint.teacher_unavailable",
                    f"Teacher '{params.teacher_id}' is unavailable.",
                )
            elif isinstance(params, RoomUnavailable):
                if params.room_id not in rooms or any(
                    slot_id not in slots for slot_id in params.slot_ids
                ):
                    self._invalid_constraint_reference(context, constraint)
                    continue
                affected = tuple(
                    lesson
                    for lessons in lessons_by_requirement.values()
                    for lesson in lessons
                    if lesson.room_id == params.room_id
                )
                self._apply_unavailability(
                    context, constraint, affected, params.slot_ids,
                    "constraint.room_unavailable",
                    f"Room '{params.room_id}' is unavailable.",
                )
            elif isinstance(params, ClassUnavailable):
                if params.class_id not in classes or any(
                    slot_id not in slots for slot_id in params.slot_ids
                ):
                    self._invalid_constraint_reference(context, constraint)
                    continue
                affected = self._matching_lessons(
                    lessons_by_requirement,
                    requirements,
                    lambda requirement: requirement.class_id == params.class_id,
                )
                self._apply_unavailability(
                    context, constraint, affected, params.slot_ids,
                    "constraint.class_unavailable",
                    f"Class '{params.class_id}' is unavailable.",
                )
            elif isinstance(params, RoomFeatureRequired):
                if params.requirement_id not in requirements:
                    self._invalid_constraint_reference(context, constraint)
                    continue
                requirement = requirements[params.requirement_id]
                if requirement.class_id not in classes or requirement.teacher_id not in teachers:
                    self._invalid_constraint_reference(context, constraint)
                    continue
                for lesson in lessons_by_requirement.get(params.requirement_id, ()):
                    room = rooms.get(lesson.room_id)
                    if room is not None and params.feature not in room.features:
                        self._apply_rule(
                            context,
                            constraint,
                            "constraint.room_feature",
                            f"Room '{room.room_id}' lacks required feature "
                            f"'{params.feature}'.",
                            (lesson,),
                        )
            elif isinstance(params, MaxClassPeriodsPerDay):
                if params.class_id not in classes:
                    self._invalid_constraint_reference(context, constraint)
                    continue
                self._check_max_periods(
                    context, constraint, params, requirements, slots,
                    lessons_by_requirement,
                )
            elif isinstance(params, TeacherPreferredSlots):
                if params.teacher_id not in teachers or any(
                    slot_id not in slots for slot_id in params.slot_ids
                ):
                    self._invalid_constraint_reference(context, constraint)
                    continue
                preferred = set(params.slot_ids)
                affected = self._matching_lessons(
                    lessons_by_requirement,
                    requirements,
                    lambda requirement: requirement.teacher_id == params.teacher_id,
                )
                for lesson in affected:
                    if lesson.slot_id not in preferred:
                        self._apply_rule(
                            context,
                            constraint,
                            "soft.teacher_preferred_slots",
                            f"Lesson is outside teacher '{params.teacher_id}' "
                            "preferred slots.",
                            (lesson,),
                        )
            elif isinstance(params, MinimizeTeacherGaps):
                if params.teacher_id is not None and params.teacher_id not in teachers:
                    self._invalid_constraint_reference(context, constraint)
                    continue
                affected = self._matching_lessons(
                    lessons_by_requirement,
                    requirements,
                    lambda requirement: (
                        params.teacher_id is None
                        or requirement.teacher_id == params.teacher_id
                    ),
                )
                for teacher_id, grouped_lessons in self._group_by_resource(
                    affected, requirements, lambda requirement: requirement.teacher_id
                ).items():
                    self._report_gaps(
                        context, constraint, grouped_lessons, slots,
                        "soft.teacher_gaps", f"Teacher '{teacher_id}'",
                    )
            elif isinstance(params, MinimizeClassGaps):
                if params.class_id is not None and params.class_id not in classes:
                    self._invalid_constraint_reference(context, constraint)
                    continue
                affected = self._matching_lessons(
                    lessons_by_requirement,
                    requirements,
                    lambda requirement: (
                        params.class_id is None
                        or requirement.class_id == params.class_id
                    ),
                )
                for class_id, grouped_lessons in self._group_by_resource(
                    affected, requirements, lambda requirement: requirement.class_id
                ).items():
                    self._report_gaps(
                        context, constraint, grouped_lessons, slots,
                        "soft.class_gaps", f"Class '{class_id}'",
                    )
            elif isinstance(params, BalanceClassDailyLoad):
                if params.class_id is not None and params.class_id not in classes:
                    self._invalid_constraint_reference(context, constraint)
                    continue
                affected = self._matching_lessons(
                    lessons_by_requirement,
                    requirements,
                    lambda requirement: (
                        params.class_id is None
                        or requirement.class_id == params.class_id
                    ),
                )
                for class_id, grouped_lessons in self._group_by_resource(
                    affected, requirements, lambda requirement: requirement.class_id
                ).items():
                    self._report_load_balance(
                        context, constraint, grouped_lessons, slots, class_id
                    )
            else:
                context.add_finding(
                    "constraint.unsupported",
                    f"Constraint '{constraint.constraint_id}' is unsupported.",
                    constraint=constraint,
                )

    @staticmethod
    def _parameters_match(constraint_type: ConstraintType, params: object) -> bool:
        expected = {
            ConstraintType.TEACHER_UNAVAILABLE: TeacherUnavailable,
            ConstraintType.ROOM_UNAVAILABLE: RoomUnavailable,
            ConstraintType.CLASS_UNAVAILABLE: ClassUnavailable,
            ConstraintType.ROOM_FEATURE_REQUIRED: RoomFeatureRequired,
            ConstraintType.MAX_CLASS_PERIODS_PER_DAY: MaxClassPeriodsPerDay,
            ConstraintType.TEACHER_PREFERRED_SLOTS: TeacherPreferredSlots,
            ConstraintType.MINIMIZE_TEACHER_GAPS: MinimizeTeacherGaps,
            ConstraintType.MINIMIZE_CLASS_GAPS: MinimizeClassGaps,
            ConstraintType.BALANCE_CLASS_DAILY_LOAD: BalanceClassDailyLoad,
        }
        parameter_type = expected.get(constraint_type)
        return parameter_type is not None and isinstance(params, parameter_type)

    @staticmethod
    def _constraint_configuration_is_valid(
        constraint: TimetableConstraint, params: object
    ) -> bool:
        if (
            not isinstance(constraint.severity, ConstraintSeverity)
            or not isinstance(constraint.weight, int)
            or isinstance(constraint.weight, bool)
            or constraint.weight <= 0
        ):
            return False
        if isinstance(
            params,
            (TeacherUnavailable, RoomUnavailable, ClassUnavailable),
        ):
            target_id = (
                params.teacher_id
                if isinstance(params, TeacherUnavailable)
                else params.room_id
                if isinstance(params, RoomUnavailable)
                else params.class_id
            )
            return (
                isinstance(target_id, str)
                and bool(target_id.strip())
                and isinstance(params.slot_ids, tuple)
                and all(
                    isinstance(slot_id, str) and bool(slot_id.strip())
                    for slot_id in params.slot_ids
                )
            )
        if isinstance(params, RoomFeatureRequired):
            return (
                isinstance(params.requirement_id, str)
                and bool(params.requirement_id.strip())
                and isinstance(params.feature, str)
                and bool(params.feature.strip())
            )
        if isinstance(params, MaxClassPeriodsPerDay):
            return (
                isinstance(params.class_id, str)
                and bool(params.class_id.strip())
                and isinstance(params.maximum, int)
                and not isinstance(params.maximum, bool)
                and params.maximum > 0
            )
        if isinstance(params, TeacherPreferredSlots):
            return (
                isinstance(params.teacher_id, str)
                and bool(params.teacher_id.strip())
                and isinstance(params.slot_ids, tuple)
                and all(
                    isinstance(slot_id, str) and bool(slot_id.strip())
                    for slot_id in params.slot_ids
                )
            )
        if isinstance(params, MinimizeTeacherGaps):
            return params.teacher_id is None or (
                isinstance(params.teacher_id, str) and bool(params.teacher_id.strip())
            )
        if isinstance(params, MinimizeClassGaps | BalanceClassDailyLoad):
            return params.class_id is None or (
                isinstance(params.class_id, str) and bool(params.class_id.strip())
            )
        return False

    @staticmethod
    def _matching_lessons(
        lessons_by_requirement: dict[str, list[LessonAssignment]],
        requirements: dict[str, TeachingRequirement],
        predicate: Callable[[TeachingRequirement], bool],
    ) -> tuple[LessonAssignment, ...]:
        return tuple(
            lesson
            for requirement_id, lessons in lessons_by_requirement.items()
            if requirement_id in requirements
            and predicate(requirements[requirement_id])
            for lesson in lessons
        )

    @staticmethod
    def _group_by_resource(
        lessons: tuple[LessonAssignment, ...],
        requirements: dict[str, TeachingRequirement],
        resource_id: Callable[[TeachingRequirement], str],
    ) -> dict[str, tuple[LessonAssignment, ...]]:
        grouped: dict[str, list[LessonAssignment]] = defaultdict(list)
        for lesson in lessons:
            requirement = requirements.get(lesson.requirement_id)
            if requirement is not None:
                grouped[resource_id(requirement)].append(lesson)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _invalid_constraint_reference(
        context: VerificationContext, constraint: TimetableConstraint
    ) -> None:
        context.add_finding(
            "constraint.invalid_reference",
            f"Constraint '{constraint.constraint_id}' references an unknown "
            "entity or time slot.",
            constraint=constraint,
        )

    @staticmethod
    def _apply_unavailability(
        context: VerificationContext,
        constraint: TimetableConstraint,
        lessons: tuple[LessonAssignment, ...],
        unavailable_slot_ids: tuple[str, ...],
        code: str,
        message: str,
    ) -> None:
        unavailable = set(unavailable_slot_ids)
        for lesson in lessons:
            if lesson.slot_id in unavailable:
                ConstraintVerificationHandler._apply_rule(
                    context, constraint, code, message, (lesson,)
                )

    @staticmethod
    def _apply_rule(
        context: VerificationContext,
        constraint: TimetableConstraint,
        code: str,
        message: str,
        lessons: tuple[LessonAssignment, ...],
        units: int = 1,
    ) -> None:
        if constraint.severity == ConstraintSeverity.SOFT:
            context.add_soft_finding(
                constraint, code, message, lessons, units
            )
        else:
            context.add_finding(
                code, message, constraint=constraint, lessons=lessons
            )

    def _check_max_periods(
        self,
        context: VerificationContext,
        constraint: TimetableConstraint,
        params: MaxClassPeriodsPerDay,
        requirements: dict[str, TeachingRequirement],
        slots: dict[str, TimeSlot],
        lessons_by_requirement: dict[str, list[LessonAssignment]],
    ) -> None:
        lessons = self._matching_lessons(
            lessons_by_requirement,
            requirements,
            lambda requirement: requirement.class_id == params.class_id,
        )
        by_day: dict[object, list[LessonAssignment]] = defaultdict(list)
        for lesson in lessons:
            slot = slots.get(lesson.slot_id)
            if slot is not None:
                by_day[slot.day].append(lesson)
        for day, daily_lessons in by_day.items():
            if len(daily_lessons) > params.maximum:
                self._apply_rule(
                    context,
                    constraint,
                    "constraint.max_class_periods_per_day",
                    f"Class '{params.class_id}' has {len(daily_lessons)} lessons "
                    f"on {day.value}; maximum is {params.maximum}.",
                    tuple(daily_lessons),
                )

    def _report_gaps(
        self,
        context: VerificationContext,
        constraint: TimetableConstraint,
        lessons: tuple[LessonAssignment, ...],
        slots: dict[str, TimeSlot],
        code: str,
        resource_label: str,
    ) -> None:
        by_day: dict[object, list[LessonAssignment]] = defaultdict(list)
        for lesson in lessons:
            slot = slots.get(lesson.slot_id)
            if slot is not None:
                by_day[slot.day].append(lesson)
        for day, daily_lessons in by_day.items():
            ordered = sorted(
                daily_lessons, key=lambda lesson: slots[lesson.slot_id].period
            )
            for previous, following in zip(ordered, ordered[1:]):
                gap = max(
                    0,
                    slots[following.slot_id].period
                    - slots[previous.slot_id].period
                    - 1,
                )
                if gap:
                    self._apply_rule(
                        context,
                        constraint,
                        code,
                        f"{resource_label} has {gap} empty period(s) on "
                        f"{day.value} between scheduled lessons.",
                        (previous, following),
                        units=gap,
                    )

    def _report_load_balance(
        self,
        context: VerificationContext,
        constraint: TimetableConstraint,
        lessons: tuple[LessonAssignment, ...],
        slots: dict[str, TimeSlot],
        class_id: str,
    ) -> None:
        daily: dict[object, list[LessonAssignment]] = defaultdict(list)
        for lesson in lessons:
            slot = slots.get(lesson.slot_id)
            if slot is not None:
                daily[slot.day].append(lesson)
        days = tuple(daily)
        units = sum(
            abs(len(daily[left]) - len(daily[right]))
            for index, left in enumerate(days)
            for right in days[index + 1 :]
        )
        if units:
            affected = tuple(
                lesson for daily_lessons in daily.values() for lesson in daily_lessons
            )
            self._apply_rule(
                context,
                constraint,
                "soft.class_daily_load_balance",
                f"Class '{class_id}' has a pairwise daily-load difference of "
                f"{units} lesson(s).",
                affected,
                units=units,
            )
