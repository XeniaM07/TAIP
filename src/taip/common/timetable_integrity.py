"""Pure domain guards shared by confirmation and independent verification."""

from collections import defaultdict
from collections.abc import Iterator
import re

from taip.common.timetable_contracts import MaxClassPeriodsPerDay, TimetableData, Weekday
from taip.common.timetable_protocols import ValidationIssue


MAX_SESSIONS_PER_REQUIREMENT = 1000
MAX_TOTAL_SESSIONS = 10_000
MAX_PERIOD = 10_000
MAX_CONSTRAINT_WEIGHT = 1_000_000
MAX_DAILY_PERIODS = 10_000
MAX_HEADCOUNT = 1_000_000
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def iter_timetable_integrity_issues(data: TimetableData) -> Iterator[ValidationIssue]:
    """Yield structural faults without provider calls or occurrence expansion.

    Clock endpoints remain optional. Only complete explicit intervals can be
    compared; adjacent intervals are allowed. A sweep reports at most one
    overlap per slot, rather than constructing all overlapping slot pairs.
    """
    total = 0
    for index, requirement in enumerate(data.requirements):
        count = requirement.sessions_per_week
        path = f"requirements[{index}].sessions_per_week"
        if type(count) is not int or count <= 0:
            yield ValidationIssue(
                "INVALID_SESSIONS",
                "Sessions per week must be a positive integer.",
                path,
            )
            continue
        total += count
        if count > MAX_SESSIONS_PER_REQUIREMENT or total > MAX_TOTAL_SESSIONS:
            yield ValidationIssue(
                "LESSON_RESOURCE_LIMIT",
                "Lesson counts exceed the supported limit of 1,000 per "
                "requirement or 10,000 total.",
                path,
            )
            return

    # Bound arithmetic operands, rather than expanding or rendering arbitrary
    # integers supplied through editable drafts or direct snapshot callers.
    for index, constraint in enumerate(data.constraints):
        path = f"constraints[{index}]"
        if type(constraint.weight) is int and constraint.weight > MAX_CONSTRAINT_WEIGHT:
            yield ValidationIssue(
                "WEIGHT_RESOURCE_LIMIT",
                "Constraint weight exceeds the supported limit of 1,000,000.",
                f"{path}.weight",
            )
        params = constraint.parameters
        if (
            isinstance(params, MaxClassPeriodsPerDay)
            and type(params.maximum) is int
            and params.maximum > MAX_DAILY_PERIODS
        ):
            yield ValidationIssue(
                "DAILY_PERIODS_RESOURCE_LIMIT",
                "Daily period maximum exceeds the supported limit of 10,000.",
                f"{path}.parameters.maximum",
            )

    for collection, attribute, code, resource_code in (
        ("classes", "student_count", "INVALID_CLASS_SIZE", "CLASS_SIZE_RESOURCE_LIMIT"),
        ("rooms", "capacity", "INVALID_ROOM_CAPACITY", "ROOM_CAPACITY_RESOURCE_LIMIT"),
    ):
        for index, item in enumerate(getattr(data, collection)):
            value = getattr(item, attribute)
            if value is not None and (type(value) is not int or value < 0):
                yield ValidationIssue(
                    code,
                    f"Known {attribute} must be a nonnegative integer.",
                    f"{collection}[{index}].{attribute}",
                )
            elif value is not None and value > MAX_HEADCOUNT:
                yield ValidationIssue(
                    resource_code,
                    f"Known {attribute} exceeds the supported limit of 1,000,000.",
                    f"{collection}[{index}].{attribute}",
                )

    subject_ids = {item.subject_id for item in data.subjects}
    for index, teacher in enumerate(data.teachers):
        path = f"teachers[{index}].subject_ids"
        if not isinstance(teacher.subject_ids, tuple):
            yield ValidationIssue(
                "INVALID_TEACHER_SUBJECTS",
                "Teacher subject references must be a tuple of subject IDs.",
                path,
            )
            continue
        for subject_index, subject_id in enumerate(teacher.subject_ids):
            if not isinstance(subject_id, str) or subject_id not in subject_ids:
                yield ValidationIssue(
                    "UNKNOWN_TEACHER_SUBJECT",
                    "Teacher references an unknown subject.",
                    f"{path}[{subject_index}]",
                )

    coordinates: set[tuple[Weekday, int]] = set()
    intervals: dict[Weekday, list[tuple[str, str, int]]] = defaultdict(list)
    for index, slot in enumerate(data.time_slots):
        path = f"time_slots[{index}]"
        valid_day = isinstance(slot.day, Weekday)
        valid_period = type(slot.period) is int and slot.period > 0
        if not valid_day:
            yield ValidationIssue(
                "INVALID_SLOT_DAY",
                "Time slot day must be a supported Weekday.",
                f"{path}.day",
            )
        if not valid_period:
            yield ValidationIssue(
                "INVALID_SLOT_PERIOD",
                "Time slot period must be a positive integer.",
                f"{path}.period",
            )
        elif slot.period > MAX_PERIOD:
            valid_period = False
            yield ValidationIssue(
                "PERIOD_RESOURCE_LIMIT",
                "Time slot period exceeds the supported limit of 10,000.",
                f"{path}.period",
            )
        if valid_day and valid_period:
            coordinate = (slot.day, slot.period)
            if coordinate in coordinates:
                yield ValidationIssue(
                    "DUPLICATE_SLOT_COORDINATES",
                    "Different time slot entries cannot share a day and period.",
                    path,
                )
            coordinates.add(coordinate)

        valid_clocks = True
        for attribute in ("start_time", "end_time"):
            value = getattr(slot, attribute)
            if value is not None and (
                not isinstance(value, str) or _CLOCK.fullmatch(value) is None
            ):
                valid_clocks = False
                yield ValidationIssue(
                    "INVALID_SLOT_TIME",
                    "Clock times must use HH:MM with hours 00-23 and minutes 00-59.",
                    f"{path}.{attribute}",
                )
        if (
            valid_clocks
            and slot.start_time is not None
            and slot.end_time is not None
        ):
            if slot.end_time <= slot.start_time:
                yield ValidationIssue(
                    "INVALID_SLOT_INTERVAL",
                    "Time slot end must be later than its start.",
                    path,
                )
            elif valid_day:
                intervals[slot.day].append((slot.start_time, slot.end_time, index))

    for daily in intervals.values():
        latest_end = ""
        for start, end, index in sorted(daily):
            if start < latest_end:
                yield ValidationIssue(
                    "OVERLAPPING_SLOT_INTERVALS",
                    "Explicit intervals on the same day cannot overlap.",
                    f"time_slots[{index}]",
                )
            latest_end = max(latest_end, end)
