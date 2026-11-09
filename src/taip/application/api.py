"""FastAPI routes for the provider-independent TAIP workflow."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


from taip.application.workflow import (
    WorkflowService,
    WorkflowStatus,
    build_default_service,
)
from taip.common.timetable_contracts import (
    BalanceClassDailyLoad,
    ClassUnavailable,
    ConstraintSeverity,
    ConstraintType,
    MaxClassPeriodsPerDay,
    MinimizeClassGaps,
    MinimizeTeacherGaps,
    RoomFeatureRequired,
    RoomUnavailable,
    TeacherPreferredSlots,
    TeacherUnavailable,
    TimetableConstraint,
    TimetableData,
    Weekday,
    Room,
    SchoolClass,
    Subject,
    Teacher,
    TeachingRequirement,
    TimeSlot,
)


class CreateSessionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)


class ExtractRequest(BaseModel):
    model_ids: list[str] | None = None


class EditDraftRequest(BaseModel):
    data: dict[str, Any]


def _json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _json(getattr(value, item.name))
            for item in fields(value)
            if item.metadata.get("serialize", True)
        }
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _data_from_json(raw: dict[str, Any]) -> TimetableData:
    params_by_type = {
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
    constraints = []
    for item in raw.get("constraints", []):
        kind = ConstraintType(item["type"])
        params = dict(item["parameters"])
        if "slot_ids" in params:
            params["slot_ids"] = tuple(params["slot_ids"])
        constraints.append(TimetableConstraint(
            constraint_id=item["constraint_id"], type=kind,
            severity=ConstraintSeverity(item.get("severity", "hard")),
            parameters=params_by_type[kind](**params),
            weight=item.get("weight", 1), source_text=item.get("source_text"),
        ))
    return TimetableData(
        classes=tuple(SchoolClass(**item) for item in raw.get("classes", [])),
        teachers=tuple(Teacher(**{**item, "subject_ids": tuple(item.get("subject_ids", []))}) for item in raw.get("teachers", [])),
        subjects=tuple(Subject(**item) for item in raw.get("subjects", [])),
        rooms=tuple(Room(**{**item, "features": tuple(item.get("features", []))}) for item in raw.get("rooms", [])),
        time_slots=tuple(TimeSlot(**{**item, "day": Weekday(item["day"])}) for item in raw.get("time_slots", [])),
        requirements=tuple(TeachingRequirement(**item) for item in raw.get("requirements", [])),
        constraints=tuple(constraints),
    )


def _default_benchmark_factory(session_id: str):
    import importlib

    service_type = getattr(importlib.import_module("taip." + "benchmark.timetable_service"), "TimetableBenchmarkService")
    return service_type(session_id)


def create_app(service: WorkflowService | None = None) -> FastAPI:
    workflow = service or build_default_service()
    if workflow.benchmark_factory is None:
        workflow.benchmark_factory = _default_benchmark_factory
    app = FastAPI(title="TAIP", version="0.1.0", description="Mockable end-to-end timetable workflow API")
    app.state.workflow = workflow
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
    )

    def get_session(session_id: str):
        try:
            return workflow.sessions.get(session_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    def run(action, *args):
        try:
            return action(*args)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/mock/demo-prompt")
    def mock_demo_prompt():
        return {"prompt": workflow.mock_demo_prompt}

    @app.post("/api/sessions", status_code=201)
    def create_session(request: CreateSessionRequest):
        session = run(workflow.create_session, request.prompt)
        return _session_json(session)

    @app.get("/api/sessions/{session_id}")
    def session_state(session_id: str):
        return _session_json(get_session(session_id))

    @app.post("/api/sessions/{session_id}/extract")
    def extract(session_id: str, request: ExtractRequest = ExtractRequest()):
        session = run(workflow.extract, session_id, request.model_ids)
        return _session_json(session)

    @app.get("/api/sessions/{session_id}/drafts/{model_id}")
    def get_draft(session_id: str, model_id: str):
        session = get_session(session_id)
        workspace = run(workflow._workspace, session, model_id)
        if workspace.draft is None:
            raise HTTPException(404, "Draft not found.")
        return _json(workspace.draft)

    @app.patch("/api/sessions/{session_id}/drafts/{model_id}")
    def edit_draft(session_id: str, model_id: str, request: EditDraftRequest):
        try:
            data = _data_from_json(request.data)
            workspace = workflow.edit_draft(session_id, model_id, data)
            return _json(workspace)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/api/sessions/{session_id}/models/{model_id}/confirm")
    def confirm(session_id: str, model_id: str):
        workspace = run(workflow.confirm, session_id, model_id)
        return _json(workspace)

    @app.post("/api/sessions/{session_id}/models/{model_id}/solve")
    def solve(session_id: str, model_id: str):
        workspace = run(workflow.solve, session_id, model_id)
        return _json(workspace)

    @app.post("/api/sessions/{session_id}/models/{model_id}/verify")
    def verify(session_id: str, model_id: str):
        workspace = run(workflow.verify, session_id, model_id)
        return _json(workspace)

    @app.get("/api/sessions/{session_id}/compare")
    def compare(session_id: str):
        summary = run(workflow.compare, session_id)
        report = run(workflow.comparison_report, session_id)
        if report is None:
            report = {
                "rows": [],
                "snapshots_comparable": None,
                "notes": ["Detailed timetable metrics are unavailable for this benchmark provider."],
            }
        payload = {
            "session_id": session_id,
            "comparison_id": summary.comparison_id,
            "problem_id": summary.problem_id,
            "models": _json(summary.model_runs),
        }
        if report is not None:
            payload["report"] = _json(report)
        return payload

    return app


def _session_json(session) -> dict[str, Any]:
    states = [workspace.status for workspace in session.models.values()]
    if WorkflowStatus.FAILED in states:
        status = WorkflowStatus.FAILED
    elif WorkflowStatus.SOLVING in states:
        status = WorkflowStatus.SOLVING
    elif WorkflowStatus.EXTRACTING in states:
        status = WorkflowStatus.EXTRACTING
    elif all(state == WorkflowStatus.COMPLETED for state in states):
        status = WorkflowStatus.COMPLETED
    elif all(state in (WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED, WorkflowStatus.COMPLETED) for state in states) and any(state == WorkflowStatus.DRAFT for state in states):
        status = WorkflowStatus.DRAFT
    elif all(state in (WorkflowStatus.CONFIRMED, WorkflowStatus.COMPLETED) for state in states) and any(state == WorkflowStatus.CONFIRMED for state in states):
        status = WorkflowStatus.CONFIRMED
    elif any(state == WorkflowStatus.COMPLETED for state in states):
        status = WorkflowStatus.COMPLETED
    else:
        status = WorkflowStatus.PENDING
    return {
        "session_id": session.session_id,
        "problem": _json(session.problem),
        "status": status.value,
        "created_at": session.created_at.isoformat(),
        "last_accessed_at": session.last_accessed_at.isoformat(),
        "models": {model_id: _json(workspace) for model_id, workspace in session.models.items()},
    }


app = create_app()
