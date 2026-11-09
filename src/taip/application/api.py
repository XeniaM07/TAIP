"""FastAPI routes for the provider-independent TAIP workflow."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from collections.abc import Callable
import json
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from taip.ai_integration.errors import AIProviderError, AIResponseError
from taip.ai_integration.response_codec import decode_timetable_data
from taip.application.session_guard import SessionOperationGuard
from taip.application.workflow import (
    WorkflowService,
    WorkflowStatus,
    ModelWorkspace,
    build_default_service,
)
from taip.common.timetable_contracts import TimetableData


class CreateSessionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)

    @field_validator("prompt")
    @classmethod
    def bounded_prompt(cls, value: str) -> str:
        try:
            valid = len(value.encode("utf-8")) <= 16384
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError("Prompt exceeds the supported UTF-8 byte limit.")
        return value


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


def _workspace_json(workspace: ModelWorkspace) -> dict[str, Any]:
    """Project current state without multiplying immutable history into every GET."""
    history_kinds = ("snapshots", "solutions", "verifications", "attempts")
    public = {
        item.name: _json(getattr(workspace, item.name))
        for item in fields(workspace)
        if item.metadata.get("serialize", True)
        and item.name not in (*history_kinds, "proposal", "verification_durations_ms")
    }
    public["proposal"] = None if workspace.proposal is None else {
        item.name: _json(getattr(workspace.proposal, item.name))
        for item in fields(workspace.proposal) if item.name != "raw_response"
    }
    public["attempts"] = _json(workspace.attempts[-5:])
    public["history_counts"] = {kind: len(getattr(workspace, kind)) for kind in history_kinds}
    return public


def _history_page(workspace: ModelWorkspace, kind: str, offset: int, limit: int) -> dict[str, Any]:
    entries = getattr(workspace, kind)
    items = []
    size = 0
    # Include complete immutable entries only; next_offset advances by entries
    # actually returned so byte-bound pages never skip evidence.
    for entry in entries[offset:offset + limit]:
        encoded = _json(entry)
        entry_size = len(json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if items and size + entry_size > 1024 * 1024 - 1024:
            break
        if entry_size > 1024 * 1024 - 1024:
            raise HTTPException(413, "A history entry exceeds the supported page size.")
        items.append(encoded)
        size += entry_size
    next_offset = offset + len(items)
    return {"kind": kind, "offset": offset, "total": len(entries), "items": items,
            "next_offset": next_offset if next_offset < len(entries) else None}


def _raw_page(raw: str | None, offset: int, limit: int) -> dict[str, Any]:
    if raw is None:
        raise HTTPException(404, "Raw response not found.")
    text = raw[offset:offset + limit]
    next_offset = offset + len(text)
    return {"offset": offset, "total": len(raw), "text": text,
            "next_offset": next_offset if next_offset < len(raw) else None}


def _data_from_json(raw: dict[str, Any]) -> TimetableData:
    return decode_timetable_data(raw)


def _default_benchmark_factory(session_id: str):
    import importlib

    service_type = getattr(importlib.import_module("taip." + "benchmark.timetable_service"), "TimetableBenchmarkService")
    return service_type(session_id)


def create_app(
    service: WorkflowService | None = None, *,
    runtime_info: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    workflow = service or build_default_service()
    if workflow.benchmark_factory is None:
        workflow.benchmark_factory = _default_benchmark_factory
    app = FastAPI(title="TAIP", version="0.1.0", description="Mockable end-to-end timetable workflow API")
    app.state.workflow = workflow
    if runtime_info is None:
        from taip.application.runtime import runtime_info_for_service

        runtime_info = lambda: runtime_info_for_service(workflow)
    app.state.runtime_info = runtime_info
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
    )
    app.add_middleware(SessionOperationGuard, workflow=workflow)

    def get_session(session_id: str):
        try:
            return workflow.sessions.get(session_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    def run(action, *args):
        try:
            return action(*args)
        except (AIProviderError, AIResponseError):
            raise
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(400, str(error)) from error

    def mutate(action, session_id: str, model_id: str, session_view: bool, *args):
        workspace = run(workflow._workspace, get_session(session_id), model_id)
        previous_failure = workspace.failure
        try:
            workspace = run(action, session_id, model_id, *args)
        except (AIProviderError, AIResponseError):
            # The facade retains the failed workspace before preserving the
            # typed exception for non-HTTP callers. Return its current state.
            pass
        except HTTPException:
            if (workspace.failure is not previous_failure and workspace.failure is not None
                    and workspace.failure.code == "unexpected_error"):
                # A provider/local bug can use ValueError or KeyError too;
                # newly recorded failure context distinguishes it from user
                # validation and prevents arbitrary exception text escaping.
                raise HTTPException(500, "Internal server error.") from None
            raise
        return _session_json(get_session(session_id)) if session_view else _workspace_json(workspace)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/runtime")
    def runtime_state():
        return runtime_info()

    @app.get("/api/mock/demo-prompt")
    def mock_demo_prompt():
        info = runtime_info()
        if info["mode"] != "mock" or info["demo_prompt"] is None:
            raise HTTPException(404, "Mock demonstration is unavailable in this runtime.")
        return {"prompt": info["demo_prompt"]}

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

    @app.get("/api/sessions/{session_id}/models/{model_id}/history")
    def model_history(
        session_id: str, model_id: str,
        kind: Literal["snapshots", "solutions", "verifications", "attempts"],
        offset: int = Query(default=0, ge=0), limit: int = Query(default=5, ge=1, le=10),
    ):
        workspace = run(workflow._workspace, get_session(session_id), model_id)
        return _history_page(workspace, kind, offset, limit)

    @app.get("/api/sessions/{session_id}/models/{model_id}/proposal/raw")
    def proposal_raw(
        session_id: str, model_id: str,
        offset: int = Query(default=0, ge=0), limit: int = Query(default=16384, ge=1, le=16384),
    ):
        workspace = run(workflow._workspace, get_session(session_id), model_id)
        if workspace.proposal is None:
            raise HTTPException(404, "Proposal not found.")
        return _raw_page(workspace.proposal.raw_response, offset, limit)

    @app.get("/api/sessions/{session_id}/models/{model_id}/attempts/{attempt_id}/raw")
    def attempt_raw(
        session_id: str, model_id: str, attempt_id: str,
        offset: int = Query(default=0, ge=0), limit: int = Query(default=16384, ge=1, le=16384),
    ):
        workspace = run(workflow._workspace, get_session(session_id), model_id)
        attempt = next((entry for entry in workspace.attempts if entry.attempt_id == attempt_id), None)
        if attempt is None:
            raise HTTPException(404, "Attempt not found.")
        return _raw_page(attempt.raw_response, offset, limit)

    @app.patch("/api/sessions/{session_id}/drafts/{model_id}")
    def edit_draft(session_id: str, model_id: str, request: EditDraftRequest, session_view: bool = False):
        try:
            data = _data_from_json(request.data)
            return mutate(workflow.edit_draft, session_id, model_id, session_view, data)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/api/sessions/{session_id}/models/{model_id}/confirm")
    def confirm(session_id: str, model_id: str, session_view: bool = False):
        return mutate(workflow.confirm, session_id, model_id, session_view)

    @app.post("/api/sessions/{session_id}/models/{model_id}/solve")
    def solve(session_id: str, model_id: str, session_view: bool = False):
        return mutate(workflow.solve, session_id, model_id, session_view)

    @app.post("/api/sessions/{session_id}/models/{model_id}/verify")
    def verify(session_id: str, model_id: str, session_view: bool = False):
        return mutate(workflow.verify, session_id, model_id, session_view)

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
    if WorkflowStatus.SOLVING in states:
        status = "SOLVING"
    elif WorkflowStatus.EXTRACTING in states:
        status = "EXTRACTING"
    elif WorkflowStatus.FAILED in states:
        status = "FAILED" if all(state == WorkflowStatus.FAILED for state in states) else "PARTIAL"
    else:
        # The least advanced workspace determines the remaining session work.
        status = next((state.value for state in (
            WorkflowStatus.PENDING, WorkflowStatus.DRAFT,
            WorkflowStatus.CONFIRMED, WorkflowStatus.COMPLETED,
        ) if state in states), "PENDING")
    return {
        "session_id": session.session_id,
        "problem": _json(session.problem),
        "status": status,
        "progress": {"total": len(states), **{
            state.value.lower(): states.count(state) for state in WorkflowStatus
        }},
        "created_at": session.created_at.isoformat(),
        "last_accessed_at": session.last_accessed_at.isoformat(),
        "models": {model_id: _workspace_json(workspace) for model_id, workspace in session.models.items()},
    }


app = create_app()
