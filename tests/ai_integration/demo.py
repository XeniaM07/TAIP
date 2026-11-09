"""Lab06 demo: ``python -m tests.ai_integration.demo`` is offline by default.

``--live`` explicitly loads configured providers at invocation time. The live
sample is confirmed only if its extraction matches every independently known
fact and has no unresolved annotations. Candidates remain AI output and pass
through the existing independent verifier without repair or retry.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import patch

from taip.ai_integration.config import ProviderConfig, load_provider_configs
from taip.ai_integration.errors import AIConfigurationError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.http_transport import HTTPJSONTransport, JSONTransport
from taip.ai_integration.mock_provider import MockAIProvider
from taip.ai_integration.monitoring import InvocationRecord
from taip.application.workflow import (
    InMemorySessionRepository,
    ModelWorkspace,
    WorkflowService,
    WorkflowStatus,
    logger as workflow_logger,
)
from taip.common.timetable_contracts import TimetableData, Weekday
from taip.common.timetable_protocols import AIProviderFactoryPort
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from tests.ai_integration.test_mock_provider import (
    EXTRACTION_RESPONSE,
    INVALID_SOLUTION_RESPONSE,
    PROMPT,
    SOLUTION_RESPONSE,
)


DEMO_PROMPT = (
    "Creează un orar pentru clasa A, care are 20 de elevi. "
    "Ana predă Matematică acestei clase, cu două lecții pe săptămână. "
    "Este disponibilă Sala A, cu 25 de locuri și fără dotări speciale. "
    "Intervalele disponibile sunt luni și marți, în fiecare zi perioada "
    "didactică 1, între 08:00 și 08:50."
)

_PUBLIC_INVOCATION_FIELDS = (
    "call_id", "operation", "provider", "model_id", "requested_model_id",
    "returned_model_id", "duration_ms", "outcome", "error_code",
    "http_status_code", "retry_after_seconds",
)


def is_expected_demo_data(data: TimetableData) -> bool:
    """Compare facts to the controlled literal sample, allowing technical IDs.

    This confirmation gate is deliberately stricter than structural draft
    validation: a valid extracted instance can still change a source fact.
    It conservatively requires distinct technical IDs across entity kinds.
    """
    if (
        len(data.classes) != 1 or len(data.teachers) != 1
        or len(data.subjects) != 1 or len(data.rooms) != 1
        or len(data.requirements) != 1 or len(data.time_slots) != 2
        or data.constraints
    ):
        return False
    school_class, = data.classes
    teacher, = data.teachers
    subject, = data.subjects
    room, = data.rooms
    requirement, = data.requirements
    identifiers = (
        school_class.class_id, teacher.teacher_id, subject.subject_id,
        room.room_id, requirement.requirement_id,
        *(slot.slot_id for slot in data.time_slots),
    )
    if not all(isinstance(value, str) and value.strip() for value in identifiers):
        return False
    if len(set(identifiers)) != len(identifiers):
        return False
    return (
        school_class.name in ("A", "clasa A", "Clasa A") and school_class.student_count == 20
        and teacher.name == "Ana" and teacher.subject_ids == (subject.subject_id,)
        and subject.name == "Matematică"
        and room.name == "Sala A" and room.capacity == 25 and not room.features
        and {slot.day for slot in data.time_slots} == {Weekday.MONDAY, Weekday.TUESDAY}
        and all(
            slot.period == 1 and slot.start_time == "08:00" and slot.end_time == "08:50"
            for slot in data.time_slots
        )
        and requirement.class_id == school_class.class_id
        and requirement.teacher_id == teacher.teacher_id
        and requirement.subject_id == subject.subject_id
        and requirement.sessions_per_week == 2
    )


def _can_confirm_live(workspace: ModelWorkspace) -> bool:
    proposal = workspace.proposal
    if (
        proposal is None or workspace.draft is None
        or proposal.missing_information or proposal.unsupported_requirements
        or not is_expected_demo_data(proposal.data)
        or workspace.problem_management is None
    ):
        return False
    return workspace.problem_management.validate(workspace.draft.draft_id).valid


def _model_summary(
    workspace: ModelWorkspace, status: str, records: list[InvocationRecord],
) -> dict[str, object]:
    failure = next((
        record for record in reversed(records)
        if record.model_id == workspace.model_id and record.outcome == "error"
    ), None)
    verification = workspace.verification
    return {
        "model_id": workspace.model_id,
        "status": status,
        "workflow_status": workspace.status.value,
        "verification_status": verification.status.name if verification else None,
        "violation_codes": sorted({issue.code for issue in verification.violations}) if verification else [],
        "error_code": (failure.error_code if failure else "workflow_error") if status == "failed" else None,
    }


def _invocation_summary(record: InvocationRecord) -> dict[str, object]:
    summary = {field: getattr(record, field) for field in _PUBLIC_INVOCATION_FIELDS}
    summary["generation_parameters"] = asdict(record.generation_parameters) if record.generation_parameters else None
    summary["usage"] = asdict(record.usage) if record.usage else None
    return summary


def _run_demo(
    factory: AIProviderFactoryPort,
    records: list[InvocationRecord],
    *,
    mode: str,
    prompt: str,
) -> dict[str, object]:
    service = WorkflowService(InMemorySessionRepository(), factory, TimetableVerifier())
    session = service.create_session(prompt)
    summaries = []
    # WorkflowService owns private errors. The console demo publishes only its
    # existing event metadata, excluding potentially private exception text.
    with patch.object(workflow_logger, "exception", side_effect=workflow_logger.error):
        service.extract(session.session_id)
        for model_id, workspace in session.models.items():
            if workspace.status == WorkflowStatus.FAILED:
                status = "failed"
            elif mode == "live" and not _can_confirm_live(workspace):
                status = "extraction_requires_review"
            else:
                try:
                    service.confirm(session.session_id, model_id)
                    service.solve(session.session_id, model_id)
                    service.verify(session.session_id, model_id)
                except Exception:
                    status = "failed"
                else:
                    status = "completed"
            summaries.append(_model_summary(workspace, status, records))
    return {
        "mode": mode,
        "models": summaries,
        "invocations": [_invocation_summary(record) for record in records],
    }


def run_mock_demo(
    *, invalid_solution: bool = False, malformed_extraction: bool = False,
) -> dict[str, object]:
    """Run known offline scripts through monitored providers and orchestration.

    The offline fixture explicitly confirms the supplied known data, including
    its documented missing/unsupported annotations. Live data uses a separate
    source prompt and must pass the strict review gate above.
    """
    records: list[InvocationRecord] = []
    provider = MockAIProvider(
        "mock-a",
        extraction_responses={PROMPT: "{malformed" if malformed_extraction else EXTRACTION_RESPONSE},
        solution_responses={"snapshot-a": INVALID_SOLUTION_RESPONSE if invalid_solution else SOLUTION_RESPONSE},
        observer=records.append,
    )
    factory = SimpleNamespace(
        MODEL_IDS=("mock-a",),
        get_provider=lambda model_id: {"mock-a": provider}[model_id],
    )
    # Only test fixture identities are deterministic; scripts still use the
    # real provider decoder, problem service and immutable snapshot lifecycle.
    with patch("taip.problem_management.service.uuid4", side_effect=["draft-a", "snapshot-a"]):
        return _run_demo(factory, records, mode="mock", prompt=PROMPT)


def run_live_demo(
    configs: Iterable[ProviderConfig], *, transport: JSONTransport,
) -> dict[str, object]:
    """Use exactly the caller's configured subset and transport, without retry."""
    records: list[InvocationRecord] = []
    factory = AIProviderFactory(configs, transport=transport, observer=records.append)
    return _run_demo(factory, records, mode="live", prompt=DEMO_PROMPT)


def main(argv: Sequence[str] | None = None) -> int:
    """Print one safe JSON report; real network access requires ``--live``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly use all configured live providers")
    parser.add_argument("--invalid-solution", action="store_true", help="Offline fixture with a rejected AI candidate")
    parser.add_argument("--malformed-extraction", action="store_true", help="Offline fixture with a monitored JSON failure")
    args = parser.parse_args(argv)
    if args.live and (args.invalid_solution or args.malformed_extraction):
        parser.error("--invalid-solution and --malformed-extraction are offline options")
    try:
        if args.live:
            configs = load_provider_configs()
            with HTTPJSONTransport() as transport:
                report = run_live_demo(configs, transport=transport)
        else:
            report = run_mock_demo(
                invalid_solution=args.invalid_solution,
                malformed_extraction=args.malformed_extraction,
            )
    except AIConfigurationError:
        report = {"mode": "live" if args.live else "mock", "error_code": "configuration_error", "models": [], "invocations": []}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    if "error_code" in report:
        return 1
    return 0 if all(model["status"] == "completed" for model in report["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
