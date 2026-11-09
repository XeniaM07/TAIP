"""Canonical constraint discriminators shared by AI encoding and decoding."""

from types import MappingProxyType
from typing import Mapping

from taip.common.timetable_contracts import (
    BalanceClassDailyLoad,
    ClassUnavailable,
    ConstraintParameters,
    ConstraintType,
    MaxClassPeriodsPerDay,
    MinimizeClassGaps,
    MinimizeTeacherGaps,
    RoomFeatureRequired,
    RoomUnavailable,
    TeacherPreferredSlots,
    TeacherUnavailable,
)


CONSTRAINT_PARAMETER_TYPES: Mapping[ConstraintType, type[ConstraintParameters]] = MappingProxyType({
    ConstraintType.TEACHER_UNAVAILABLE: TeacherUnavailable,
    ConstraintType.ROOM_UNAVAILABLE: RoomUnavailable,
    ConstraintType.CLASS_UNAVAILABLE: ClassUnavailable,
    ConstraintType.ROOM_FEATURE_REQUIRED: RoomFeatureRequired,
    ConstraintType.MAX_CLASS_PERIODS_PER_DAY: MaxClassPeriodsPerDay,
    ConstraintType.TEACHER_PREFERRED_SLOTS: TeacherPreferredSlots,
    ConstraintType.MINIMIZE_TEACHER_GAPS: MinimizeTeacherGaps,
    ConstraintType.MINIMIZE_CLASS_GAPS: MinimizeClassGaps,
    ConstraintType.BALANCE_CLASS_DAILY_LOAD: BalanceClassDailyLoad,
})
