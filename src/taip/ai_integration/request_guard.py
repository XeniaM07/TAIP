"""Bound untrusted request resources without interpreting or rewriting text."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math

from taip.ai_integration.errors import AIResponseError
from taip.common.timetable_contracts import TimetableData


def _invalid(path: str = "$") -> AIResponseError:
    return AIResponseError("invalid_request", path, "Expected bounded JSON-compatible input.")


def _too_large(path: str = "$") -> AIResponseError:
    return AIResponseError("input_too_large", path, "Input exceeds the configured resource limit.")


def validate_source_prompt(prompt: object, *, max_bytes: int) -> None:
    if not isinstance(prompt, str):
        raise _invalid("$.prompt")
    if len(prompt) > max_bytes:
        raise _too_large("$.prompt")
    try:
        size = len(prompt.encode("utf-8"))
    except UnicodeError:
        raise _invalid("$.prompt") from None
    if size > max_bytes:
        raise _too_large("$.prompt")
    if not prompt.strip():
        raise _invalid("$.prompt")


def validate_domain_input(
    value: object, *, max_bytes: int, max_nodes: int = 10000, max_depth: int = 32,
) -> None:
    """Preflight strings, numbers and node counts before JSON allocation.

    This is an operational budget, not a timetable feasibility check. The
    stack is bounded before expanding collections and never uses object repr.
    """
    stack = [(value, 0)]
    nodes = 0
    used = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise _too_large()
        if isinstance(item, Enum):
            stack.append((item.value, depth + 1))
        elif isinstance(item, str):
            if len(item) > max_bytes - used:
                raise _too_large()
            try:
                used += len(item.encode("utf-8"))
            except UnicodeError:
                raise _invalid() from None
        elif item is None or type(item) is bool:
            pass
        elif type(item) is int:
            # An integer's magnitude must not amplify a tiny object into a
            # huge decimal allocation during serialization.
            used += max(1, item.bit_length() * 30103 // 100000)
        elif type(item) is float:
            if not math.isfinite(item):
                raise _invalid()
        elif is_dataclass(item) and not isinstance(item, type):
            definitions = fields(item)
            if len(definitions) > max_nodes - nodes - len(stack):
                raise _too_large()
            stack.extend((getattr(item, field.name), depth + 1) for field in definitions)
        elif isinstance(item, Mapping):
            if len(item) * 2 > max_nodes - nodes - len(stack):
                raise _too_large()
            for key, child in item.items():
                if not isinstance(key, str):
                    raise _invalid()
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            if len(item) > max_nodes - nodes - len(stack):
                raise _too_large()
            stack.extend((child, depth + 1) for child in item)
        else:
            raise _invalid()
        if used > max_bytes:
            raise _too_large()


def measure_request(body: Mapping[str, object], *, max_bytes: int) -> int:
    """Count the actual compact UTF-8 JSON body without joining its chunks."""
    validate_domain_input(body, max_bytes=max_bytes)
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    size = 0
    try:
        for part in encoder.iterencode(body):
            size += len(part.encode("utf-8"))
            if size > max_bytes:
                raise _too_large()
    except AIResponseError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None
    return size


def validate_lesson_budget(data: TimetableData) -> None:
    """Reject numeric amplification before downstream range/set allocation.

    Up to 1,000 occurrences per requirement and 10,000 in total are supported
    by this AI boundary. Nonpositive values remain for domain validation.
    """
    total = 0
    for requirement in data.requirements:
        count = requirement.sessions_per_week
        if type(count) is not int:
            raise _invalid()
        if count > 1000:
            raise AIResponseError("resource_limit", "$.data.requirements", "Lesson count exceeds the supported resource budget.")
        total += max(0, count)
        if total > 10000:
            raise AIResponseError("resource_limit", "$.data.requirements", "Total lesson count exceeds the supported resource budget.")
