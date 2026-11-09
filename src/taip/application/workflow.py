"""Mockable application workflow, session repository, and per-model state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import RLock
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from taip.common.timetable_contracts import (
    ClassUnavailable,
    ConstraintSeverity,
    ConstraintType,
    ExtractionProposal,
    LessonAssignment,
    ModelRunResult,
    OriginalProblem,
    ProblemDraft,
    Room,
    SchoolClass,
    Subject,
    Teacher,
    TeachingRequirement,
    TimeSlot,
    TimetableData,
    TimetableProblemSnapshot,
    TimetableSolution,
    TimetableVerificationResult,
    Weekday,
)
from taip.common.timetable_protocols import AIProviderPort, VerificationPort
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from taip.common.timetable_protocols import EditOperation, ProblemManagementPort
from taip.problem_management.repository import InMemoryProblemRepository
from taip.problem_management.service import TimetableProblemService

logger = logging.getLogger(__name__)


class WorkflowStatus(str, Enum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SOLVING = "SOLVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class ModelWorkspace:
    model_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    draft: ProblemDraft | None = None
    snapshot: TimetableProblemSnapshot | None = None
    solution: TimetableSolution | None = None
    verification: TimetableVerificationResult | None = None
    extraction_duration_ms: float | None = None
    solving_duration_ms: float | None = None
    error: str | None = None
    edit_count: int = 0
    problem_management: ProblemManagementPort | None = field(default=None, repr=False, compare=False, metadata={"serialize": False})

    _TRANSITIONS = {
        WorkflowStatus.PENDING: {WorkflowStatus.EXTRACTING},
        WorkflowStatus.EXTRACTING: {WorkflowStatus.DRAFT, WorkflowStatus.FAILED},
        WorkflowStatus.DRAFT: {WorkflowStatus.EXTRACTING, WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED, WorkflowStatus.FAILED},
        WorkflowStatus.CONFIRMED: {WorkflowStatus.DRAFT, WorkflowStatus.SOLVING, WorkflowStatus.CONFIRMED, WorkflowStatus.FAILED},
        WorkflowStatus.SOLVING: {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED},
        WorkflowStatus.COMPLETED: {WorkflowStatus.DRAFT, WorkflowStatus.SOLVING, WorkflowStatus.EXTRACTING},
        WorkflowStatus.FAILED: {WorkflowStatus.EXTRACTING, WorkflowStatus.FAILED},
    }

    def transition(self, next_status: WorkflowStatus) -> None:
        if next_status not in self._TRANSITIONS[self.status]:
            raise ValueError(f"Invalid workflow transition: {self.status.value} -> {next_status.value}.")
        previous = self.status
        self.status = next_status
        logger.info("workflow.transition model_id=%s from=%s to=%s", self.model_id, previous.value, next_status.value)


@dataclass
class WorkSession:
    session_id: str
    problem: OriginalProblem
    models: dict[str, ModelWorkspace]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionRepository(Protocol):
    def create(self, session: WorkSession) -> None: ...
    def get(self, session_id: str) -> WorkSession: ...


class InMemorySessionRepository:
    """Thread-safe, expiring storage. Process restart intentionally clears it."""

    def __init__(self, ttl: timedelta = timedelta(hours=2)) -> None:
        self.ttl = ttl
        self._sessions: dict[str, WorkSession] = {}
        self._lock = RLock()

    def _purge_expired(self, now: datetime) -> None:
        cutoff = now - self.ttl
        for key, session in tuple(self._sessions.items()):
            if session.last_accessed_at < cutoff:
                del self._sessions[key]

    def create(self, session: WorkSession) -> None:
        with self._lock:
            self._purge_expired(datetime.now(UTC))
            self._sessions[session.session_id] = session

    def get(self, session_id: str) -> WorkSession:
        with self._lock:
            now = datetime.now(UTC)
            self._purge_expired(now)
            try:
                session = self._sessions[session_id]
            except KeyError as error:
                raise KeyError("Session not found or expired.") from error
            session.last_accessed_at = now
            return session


class ProviderFactory(Protocol):
    def get_provider(self, model_id: str) -> AIProviderPort: ...


class MockTimetableProvider:
    """Deterministic provider for local flow demonstrations only."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def extract(self, problem: OriginalProblem) -> ExtractionProposal:
        # Small, valid school timetable used by the demo regardless of prompt.
        data = TimetableData(
            classes=(SchoolClass("class-1", "Class 1", 24), SchoolClass("class-2", "Class 2", 22)),
            teachers=(Teacher("teacher-1", "Teacher 1", ("subject-1",)), Teacher("teacher-2", "Teacher 2", ("subject-1",))),
            subjects=(Subject("subject-1", "Mathematics"),),
            rooms=(Room("room-1", "Room 1", 30), Room("room-2", "Room 2", 30)),
            time_slots=tuple(TimeSlot(f"mon-{period}", Weekday.MONDAY, period) for period in range(1, 5)),
            requirements=(
                TeachingRequirement("req-1", "class-1", "subject-1", "teacher-1", 1),
                TeachingRequirement("req-2", "class-2", "subject-1", "teacher-2", 1),
            ),
        )
        return ExtractionProposal(str(uuid4()), problem.problem_id, self.model_id, data)

    def solve(self, snapshot: TimetableProblemSnapshot) -> TimetableSolution:
        rooms = snapshot.data.rooms
        slots = snapshot.data.time_slots
        if not rooms or not slots:
            raise ValueError("A timetable needs at least one room and time slot.")
        assignments = []
        index = 0
        for requirement in snapshot.data.requirements:
            for occurrence in range(requirement.sessions_per_week):
                if index >= len(slots):
                    raise ValueError("Mock solver ran out of available time slots.")
                assignments.append(LessonAssignment(
                    requirement.requirement_id, occurrence,
                    slots[index].slot_id, rooms[index % len(rooms)].room_id,
                ))
                index += 1
        return TimetableSolution(str(uuid4()), snapshot.snapshot_id, self.model_id, tuple(assignments))


class MockProviderFactory:
    MODEL_IDS = ("model-a", "model-b", "model-c")

    def get_provider(self, model_id: str) -> MockTimetableProvider:
        if model_id not in self.MODEL_IDS:
            raise KeyError(f"Unknown model '{model_id}'.")
        return MockTimetableProvider(model_id)


class WorkflowService:
    """Facade for lifecycle operations; dependencies are supplied via ports."""

    def __init__(
        self,
        sessions: SessionRepository,
        providers: ProviderFactory,
        verifier: VerificationPort,
    ) -> None:
        self.sessions = sessions
        self.providers = providers
        self.verifier = verifier

    def create_session(self, prompt: str) -> WorkSession:
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty.")
        session_id = str(uuid4())
        session = WorkSession(
            session_id, OriginalProblem(session_id, prompt),
            {
                model_id: ModelWorkspace(
                    model_id,
                    problem_management=TimetableProblemService(InMemoryProblemRepository()),
                )
                for model_id in MockProviderFactory.MODEL_IDS
            },
        )
        self.sessions.create(session)
        logger.info("session.created session_id=%s", session_id)
        return session

    def extract(self, session_id: str, model_ids: list[str] | None = None) -> WorkSession:
        session = self.sessions.get(session_id)
        targets = model_ids or list(session.models)
        for model_id in targets:
            workspace = self._workspace(session, model_id)
            workspace.transition(WorkflowStatus.EXTRACTING)
            started = perf_counter()
            try:
                proposal = self.providers.get_provider(model_id).extract(session.problem)
                assert workspace.problem_management is not None
                workspace.draft = workspace.problem_management.create_draft(proposal)
                workspace.transition(WorkflowStatus.DRAFT)
                workspace.error = None
            except Exception as error:
                workspace.transition(WorkflowStatus.FAILED)
                workspace.error = str(error)
                logger.exception("session.extraction_failed session_id=%s model_id=%s", session_id, model_id)
            workspace.extraction_duration_ms = round((perf_counter() - started) * 1000, 3)
        return session

    def edit_draft(self, session_id: str, model_id: str, data: TimetableData) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.draft is None or workspace.status not in (WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED, WorkflowStatus.COMPLETED):
            raise ValueError("Extract a draft before editing it.")
        assert workspace.problem_management is not None
        self._replace_draft_data(workspace, data)
        workspace.draft = workspace.problem_management.get_draft(workspace.draft.draft_id)
        workspace.snapshot = None
        workspace.solution = None
        workspace.verification = None
        workspace.transition(WorkflowStatus.DRAFT)
        workspace.edit_count += 1
        logger.info("session.draft_edited session_id=%s model_id=%s revision=%s", session_id, model_id, workspace.draft.revision)
        return workspace

    def confirm(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.draft is None:
            raise ValueError("Extract a draft before confirming it.")
        assert workspace.problem_management is not None
        workspace.snapshot = workspace.problem_management.confirm(
            workspace.draft.draft_id, session.problem.prompt
        )
        workspace.solution = None
        workspace.verification = None
        workspace.transition(WorkflowStatus.CONFIRMED)
        logger.info("session.confirmed session_id=%s model_id=%s version=%s", session_id, model_id, workspace.snapshot.version)
        return workspace

    def solve(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.snapshot is None or workspace.status != WorkflowStatus.CONFIRMED:
            raise ValueError("Confirm the draft before solving.")
        workspace.transition(WorkflowStatus.SOLVING)
        started = perf_counter()
        try:
            workspace.solution = self.providers.get_provider(model_id).solve(workspace.snapshot)
            workspace.transition(WorkflowStatus.COMPLETED)
            workspace.error = None
        except Exception as error:
            workspace.transition(WorkflowStatus.FAILED)
            workspace.error = str(error)
            logger.exception("session.solve_failed session_id=%s model_id=%s", session_id, model_id)
            raise
        finally:
            workspace.solving_duration_ms = round((perf_counter() - started) * 1000, 3)
        return workspace

    def verify(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.snapshot is None or workspace.solution is None:
            raise ValueError("Solve the confirmed draft before verification.")
        workspace.verification = self.verifier.verify(workspace.snapshot, workspace.solution)
        logger.info("session.verified session_id=%s model_id=%s status=%s", session_id, model_id, workspace.verification.status.value)
        return workspace

    def compare(self, session_id: str) -> list[ModelRunResult]:
        session = self.sessions.get(session_id)
        return [ModelRunResult(
            model_id=model_id,
            proposal_id=workspace.draft.proposal_id if workspace.draft else None,
            snapshot_id=workspace.snapshot.snapshot_id if workspace.snapshot else None,
            solution_id=workspace.solution.solution_id if workspace.solution else None,
            verification_status=workspace.verification.status if workspace.verification else None,
            extraction_duration_ms=int(workspace.extraction_duration_ms) if workspace.extraction_duration_ms is not None else None,
            solving_duration_ms=int(workspace.solving_duration_ms) if workspace.solving_duration_ms is not None else None,
            edit_count=workspace.edit_count,
            error=workspace.error,
        ) for model_id, workspace in session.models.items()]

    @staticmethod
    def _workspace(session: WorkSession, model_id: str) -> ModelWorkspace:
        try:
            return session.models[model_id]
        except KeyError as error:
            raise KeyError(f"Unknown model '{model_id}'.") from error

    @staticmethod
    def _replace_draft_data(workspace: ModelWorkspace, data: TimetableData) -> None:
        """Apply editor changes through the problem-management port."""
        assert workspace.draft is not None and workspace.problem_management is not None
        from dataclasses import asdict
        import json

        current = workspace.draft.data
        collections = {
            "class": (current.classes, data.classes, "class_id"),
            "teacher": (current.teachers, data.teachers, "teacher_id"),
            "subject": (current.subjects, data.subjects, "subject_id"),
            "room": (current.rooms, data.rooms, "room_id"),
            "time_slot": (current.time_slots, data.time_slots, "slot_id"),
            "requirement": (current.requirements, data.requirements, "requirement_id"),
            "constraint": (current.constraints, data.constraints, "constraint_id"),
        }

        def encoded(entity) -> str:
            def normalize(value):
                if hasattr(value, "value"):
                    return value.value
                if isinstance(value, dict):
                    return {key: normalize(item) for key, item in value.items()}
                if isinstance(value, (tuple, list)):
                    return [normalize(item) for item in value]
                return value
            return json.dumps(normalize(asdict(entity)))

        for entity_type, (before, after, id_field) in collections.items():
            before_by_id = {getattr(item, id_field): item for item in before}
            after_by_id = {getattr(item, id_field): item for item in after}
            for entity_id, old in before_by_id.items():
                if entity_id not in after_by_id:
                    operation = EditOperation(str(uuid4()), entity_type, entity_id, "remove", encoded(old), None)
                    workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)
                elif old != after_by_id[entity_id]:
                    operation = EditOperation(str(uuid4()), entity_type, entity_id, "update", encoded(old), encoded(after_by_id[entity_id]))
                    workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)
            for entity_id, new in after_by_id.items():
                if entity_id not in before_by_id:
                    operation = EditOperation(str(uuid4()), entity_type, entity_id, "add", None, encoded(new))
                    workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)


def build_default_service() -> WorkflowService:
    return WorkflowService(InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier())
