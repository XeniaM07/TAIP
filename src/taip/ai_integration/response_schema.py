"""Provider output schemas derived from the canonical timetable dataclasses.

These schemas describe JSON shape, not mathematical feasibility. Nullable
values are explicit and every object forbids extra fields. Each call returns
independent dictionaries so provider-specific serialization cannot leak state.
"""

from dataclasses import fields, is_dataclass
from enum import Enum
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from taip.ai_integration.constraint_parameters import CONSTRAINT_PARAMETER_TYPES
from taip.common.timetable_contracts import (
    LessonAssignment,
    MissingInformation,
    TimetableConstraint,
    TimetableData,
)


def _object_schema(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _record_schema(
    record_type: type, overrides: dict[str, object] | None = None
) -> dict[str, object]:
    hints = get_type_hints(record_type)
    replacements = overrides or {}
    return _object_schema({
        field.name: replacements[field.name] if field.name in replacements else _schema(hints[field.name])
        for field in fields(record_type)
    })


def _schema(annotation: object) -> dict[str, object]:
    if annotation is TimetableConstraint:
        return {"anyOf": [
            _record_schema(TimetableConstraint, {
                "type": {"type": "string", "enum": [kind.value]},
                "parameters": _record_schema(parameters),
            })
            for kind, parameters in CONSTRAINT_PARAMETER_TYPES.items()
        ]}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is type(None):
        return {"type": "null"}
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        variants = get_args(annotation)
        if len(variants) != 2 or type(None) not in variants:
            raise TypeError("Canonical union requires an explicit discriminator")
        return {"anyOf": [_schema(variant) for variant in variants]}
    if origin is tuple:
        element_type, repeated = get_args(annotation)
        if repeated is not Ellipsis:
            raise TypeError("Canonical collection must be a homogeneous tuple")
        return {"type": "array", "items": _schema(element_type)}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"type": "string", "enum": [member.value for member in annotation]}
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _record_schema(annotation)
    raise TypeError("Unsupported canonical field type")


def extraction_json_schema() -> dict[str, object]:
    """Request domain facts and uncertainty, with no workflow identifiers."""
    return _object_schema({
        "data": _schema(TimetableData),
        "missing_information": _schema(tuple[MissingInformation, ...]),
        "unsupported_requirements": _schema(tuple[str, ...]),
    })


def solution_json_schema() -> dict[str, object]:
    """Request assignments while preserving infeasible attempts for verification."""
    return _object_schema({"assignments": _schema(tuple[LessonAssignment, ...])})
