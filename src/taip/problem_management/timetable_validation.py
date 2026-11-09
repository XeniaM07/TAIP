"""Validation for timetable problem drafts."""

from taip.common.timetable_contracts import (
    ProblemDraft,
    ConstraintType,
    TeacherUnavailable,
    RoomUnavailable,
    ClassUnavailable,
    RoomFeatureRequired,
    MaxClassPeriodsPerDay,
    TeacherPreferredSlots,
    MinimizeTeacherGaps,
    MinimizeClassGaps,
    BalanceClassDailyLoad,
)
from taip.common.timetable_protocols import (
    DraftValidationResult,
    ValidationIssue,
)


class TimetableDraftValidator:
    """Validate timetable entities and their references."""

    def validate(self, draft: ProblemDraft) -> DraftValidationResult:
        data = draft.data
        issues: list[ValidationIssue] = []

        def add_issue(code: str, message: str, path: str) -> None:
            issues.append(ValidationIssue(code, message, path))

        # Required collections
        for collection in (
            "classes",
            "teachers",
            "subjects",
            "rooms",
            "time_slots",
            "requirements",
        ):
            if not getattr(data, collection):
                add_issue(
                    "MISSING_DATA",
                    f"At least one {collection} entry is required.",
                    collection,
                )

        # Check identifiers and duplicates
        id_fields = {
            "classes": "class_id",
            "teachers": "teacher_id",
            "subjects": "subject_id",
            "rooms": "room_id",
            "time_slots": "slot_id",
            "requirements": "requirement_id",
            "constraints": "constraint_id",
        }

        for collection, id_field in id_fields.items():
            seen = set()

            for index, entity in enumerate(getattr(data, collection)):
                entity_id = getattr(entity, id_field)

                if not isinstance(entity_id, str) or not entity_id.strip():
                    add_issue(
                        "INVALID_ID",
                        f"Invalid identifier in {collection}.",
                        f"{collection}[{index}].{id_field}",
                    )
                elif entity_id in seen:
                    add_issue(
                        "DUPLICATE_ID",
                        f"Duplicate identifier '{entity_id}'.",
                        f"{collection}[{index}].{id_field}",
                    )

                seen.add(entity_id)

        # Reference sets
        class_ids = {item.class_id for item in data.classes}
        teacher_ids = {item.teacher_id for item in data.teachers}
        subject_ids = {item.subject_id for item in data.subjects}
        room_ids = {room.room_id for room in data.rooms}

        # Teaching requirements
        for index, requirement in enumerate(data.requirements):
            path = f"requirements[{index}]"

            if requirement.class_id not in class_ids:
                add_issue(
                    "UNKNOWN_CLASS",
                    f"Class '{requirement.class_id}' does not exist.",
                    f"{path}.class_id",
                )

            if requirement.teacher_id not in teacher_ids:
                add_issue(
                    "UNKNOWN_TEACHER",
                    f"Teacher '{requirement.teacher_id}' does not exist.",
                    f"{path}.teacher_id",
                )

            if requirement.subject_id not in subject_ids:
                add_issue(
                    "UNKNOWN_SUBJECT",
                    f"Subject '{requirement.subject_id}' does not exist.",
                    f"{path}.subject_id",
                )

            if (
                not isinstance(requirement.sessions_per_week, int)
                or isinstance(requirement.sessions_per_week, bool)
                or requirement.sessions_per_week <= 0
            ):
                add_issue(
                    "INVALID_SESSIONS",
                    "Sessions per week must be a positive integer.",
                    f"{path}.sessions_per_week",
                )
        slot_ids = {slot.slot_id for slot in data.time_slots}
        requirement_ids = {
            requirement.requirement_id
            for requirement in data.requirements
        }

        parameter_types = {
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

        for index, constraint in enumerate(data.constraints):
            path = f"constraints[{index}]"
            params = constraint.parameters

            expected_type = parameter_types.get(constraint.type)

            if expected_type is None or not isinstance(params, expected_type):
                add_issue(
                    "INVALID_CONSTRAINT_PARAMETERS",
                    "Constraint type does not match its parameters.",
                    f"{path}.parameters",
                )
                continue

            if (
                not isinstance(constraint.weight, int)
                or isinstance(constraint.weight, bool)
                or constraint.weight <= 0
            ):
                add_issue(
                    "INVALID_WEIGHT",
                    "Constraint weight must be a positive integer.",
                    f"{path}.weight",
                )

            if hasattr(params, "teacher_id"):
                teacher_id = params.teacher_id
                if teacher_id is not None and teacher_id not in teacher_ids:
                    add_issue(
                        "UNKNOWN_TEACHER",
                        f"Teacher '{teacher_id}' does not exist.",
                        f"{path}.parameters.teacher_id",
                    )

            if hasattr(params, "class_id"):
                class_id = params.class_id
                if class_id is not None and class_id not in class_ids:
                    add_issue(
                        "UNKNOWN_CLASS",
                        f"Class '{class_id}' does not exist.",
                        f"{path}.parameters.class_id",
                    )

            if hasattr(params, "room_id"):
                if params.room_id not in room_ids:
                    add_issue(
                        "UNKNOWN_ROOM",
                        f"Room '{params.room_id}' does not exist.",
                        f"{path}.parameters.room_id",
                    )

            if hasattr(params, "requirement_id"):
                if params.requirement_id not in requirement_ids:
                    add_issue(
                        "UNKNOWN_REQUIREMENT",
                        f"Requirement '{params.requirement_id}' does not exist.",
                        f"{path}.parameters.requirement_id",
                    )

            if hasattr(params, "slot_ids"):
                for slot_id in params.slot_ids:
                    if slot_id not in slot_ids:
                        add_issue(
                            "UNKNOWN_SLOT",
                            f"Time slot '{slot_id}' does not exist.",
                            f"{path}.parameters.slot_ids",
                        )

            if isinstance(params, MaxClassPeriodsPerDay):
                if (
                    not isinstance(params.maximum, int)
                    or isinstance(params.maximum, bool)
                    or params.maximum <= 0
                ):
                    add_issue(
                        "INVALID_MAXIMUM",
                        "Maximum periods must be a positive integer.",
                        f"{path}.parameters.maximum",
                    )

            if isinstance(params, RoomFeatureRequired):
                if not params.feature.strip():
                    add_issue(
                        "INVALID_FEATURE",
                        "Required room feature cannot be empty.",
                        f"{path}.parameters.feature",
                    )
        return DraftValidationResult(
            valid=len(issues) == 0,
            issues=tuple(issues),
        )
