"""Small deterministic ground-truth set for timetable extraction comparisons."""

from taip.benchmark.models import TimetableReferenceCase
from taip.common.timetable_contracts import (
    ConstraintSeverity,
    ConstraintType,
    Room,
    MaxClassPeriodsPerDay,
    RoomFeatureRequired,
    SchoolClass,
    Subject,
    Teacher,
    TeacherUnavailable,
    TeachingRequirement,
    TimeSlot,
    TimetableData,
    TimetableConstraint,
    Weekday,
)


TIMETABLE_REFERENCE_CASES = (
    TimetableReferenceCase(
        problem_id="school-demo",
        prompt=(
            "Fă un orar pentru clasele a V-a A și a VI-a A. Informatica se "
            "ține în laborator, iar profesorul de matematică nu este disponibil "
            "luni la ora 3."
        ),
        data=TimetableData(
            classes=(SchoolClass("5A", "Clasa a V-a A", 25),),
            teachers=(
                Teacher("t-math", "Profesor matematică", ("math",)),
                Teacher("t-info", "Profesor informatică", ("info",)),
            ),
            subjects=(Subject("math", "Matematică"), Subject("info", "Informatică")),
            rooms=(
                Room("r101", "Sala 101", 30),
                Room("lab", "Laborator informatică", 28, ("computers",)),
            ),
            time_slots=(
                TimeSlot("mon-1", Weekday.MONDAY, 1),
                TimeSlot("mon-2", Weekday.MONDAY, 2),
                TimeSlot("mon-3", Weekday.MONDAY, 3),
            ),
            requirements=(
                TeachingRequirement("req-1", "5A", "math", "t-math", 1),
                TeachingRequirement("req-2", "5A", "info", "t-info", 1),
            ),
            constraints=(
                TimetableConstraint(
                    "ref-computers",
                    ConstraintType.ROOM_FEATURE_REQUIRED,
                    ConstraintSeverity.HARD,
                    RoomFeatureRequired("req-2", "computers"),
                ),
                TimetableConstraint(
                    "ref-math-away",
                    ConstraintType.TEACHER_UNAVAILABLE,
                    ConstraintSeverity.HARD,
                    TeacherUnavailable("t-math", ("mon-3",)),
                ),
            ),
        ),
    ),
    TimetableReferenceCase(
        problem_id="daily-class-limit",
        prompt="Clasa a VII-a B nu poate avea mai mult de 5 ore într-o zi.",
        data=TimetableData(
            classes=(SchoolClass("7B", "Clasa a VII-a B", 26),),
            teachers=(Teacher("t-math", "Profesor matematică", ("math",)),),
            subjects=(Subject("math", "Matematică"),),
            rooms=(Room("room-7b", "Sala 207", 30),),
            time_slots=tuple(
                TimeSlot(
                    f"mon-{period}",
                    Weekday.MONDAY,
                    period,
                )
                for period in range(1, 7)
            ),
            requirements=(
                TeachingRequirement("req-7b-math", "7B", "math", "t-math", 3),
            ),
            constraints=(
                TimetableConstraint(
                    "ref-daily-limit",
                    ConstraintType.MAX_CLASS_PERIODS_PER_DAY,
                    ConstraintSeverity.HARD,
                    MaxClassPeriodsPerDay("7B", 5),
                ),
            ),
        ),
    ),
)


def get_reference_case(problem_id: str) -> TimetableReferenceCase | None:
    """Find a reference interpretation by its stable problem identifier."""
    return next(
        (case for case in TIMETABLE_REFERENCE_CASES if case.problem_id == problem_id),
        None,
    )
