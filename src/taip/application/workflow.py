"""Mockable application workflow, session repository, and per-model state."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import RLock
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from taip.ai_integration.errors import AIProviderError, AIResponseError
from taip.common.ai_execution import (
    AIAttempt, AICallFailure, AICallOutcome, AIExecutionPort, classify_ai_failure,
)
from taip.common.timetable_contracts import (
    ClassUnavailable,
    ConstraintSeverity,
    ConstraintType,
    ExtractionProposal,
    LessonAssignment,
    MissingInformation,
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
from taip.common.timetable_protocols import (
    AIProviderFactoryPort,
    AIProviderPort,
    BenchmarkPort,
    ComparisonRun,
    EditOperation,
    ProblemManagementPort,
    VerificationPort,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from taip.problem_management.repository import InMemoryProblemRepository
from taip.problem_management.service import TimetableProblemService
from taip.orchestration.timetable_execution import InlineAIExecution

logger = logging.getLogger(__name__)

MOCK_MODEL_IDS = ("gemini-mock", "groq-llama-mock", "openrouter-qwen-mock")
MOCK_DEMO_PROMPT = (
    "Demonstrație TAIP cu date explicite: clasa Clasa 1 are 24 de elevi și clasa Clasa 2 are 22 de elevi. "
    "Disciplina este Matematică. Profesorul Profesor 1 predă la Clasa 1, iar Profesorul 2 predă la Clasa 2; "
    "ambii pot preda Matematică. Sălile Sala 1 și Sala 2 au fiecare capacitatea 30. "
    "Ziua este luni, cu intervalele luni-1, luni-2, luni-3 și luni-4. "
    "Cerințele sunt o oră de Matematică pe săptămână pentru fiecare clasă, cu profesorul indicat."
)


class WorkflowStatus(str, Enum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SOLVING = "SOLVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class _PendingDraftEdit:
    """Resume local port writes without regenerating commands or paid output."""

    edit_id: str
    operations: tuple[EditOperation, ...]
    revision_started: bool
    applied_count: int = 0
    recorded_count: int = 0


@dataclass
class ModelWorkspace:
    model_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    proposal: ExtractionProposal | None = None
    draft: ProblemDraft | None = None
    snapshot: TimetableProblemSnapshot | None = None
    snapshots: list[TimetableProblemSnapshot] = field(default_factory=list)
    solution: TimetableSolution | None = None
    solutions: list[TimetableSolution] = field(default_factory=list)
    verification: TimetableVerificationResult | None = None
    verifications: list[TimetableVerificationResult] = field(default_factory=list)
    extraction_duration_ms: float | None = None
    solving_duration_ms: float | None = None
    verification_duration_ms: float | None = None
    verification_durations_ms: list[float] = field(default_factory=list)
    error: str | None = None
    failure: AICallFailure | None = None
    attempts: list[AIAttempt] = field(default_factory=list)
    edit_count: int = 0
    problem_management: ProblemManagementPort | None = field(default=None, repr=False, compare=False, metadata={"serialize": False})
    _extraction_recorded: bool = field(default=False, repr=False, compare=False, metadata={"serialize": False})
    _solution_recorded: bool = field(default=False, repr=False, compare=False, metadata={"serialize": False})
    _verification_recorded: bool = field(default=False, repr=False, compare=False, metadata={"serialize": False})
    _pending_edit: _PendingDraftEdit | None = field(default=None, repr=False, compare=False, metadata={"serialize": False})

    _TRANSITIONS = {
        WorkflowStatus.PENDING: {WorkflowStatus.EXTRACTING},
        WorkflowStatus.EXTRACTING: {WorkflowStatus.DRAFT, WorkflowStatus.FAILED},
        WorkflowStatus.DRAFT: {WorkflowStatus.EXTRACTING, WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED, WorkflowStatus.FAILED},
        WorkflowStatus.CONFIRMED: {WorkflowStatus.DRAFT, WorkflowStatus.SOLVING, WorkflowStatus.CONFIRMED, WorkflowStatus.FAILED},
        WorkflowStatus.SOLVING: {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED},
        WorkflowStatus.COMPLETED: {WorkflowStatus.DRAFT, WorkflowStatus.SOLVING, WorkflowStatus.EXTRACTING, WorkflowStatus.FAILED},
        WorkflowStatus.FAILED: {WorkflowStatus.EXTRACTING, WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED, WorkflowStatus.COMPLETED, WorkflowStatus.FAILED},
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
    comparison_id: str
    benchmark: BenchmarkPort = field(repr=False, compare=False, metadata={"serialize": False})
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionRepository(Protocol):
    def create(self, session: WorkSession) -> None: ...
    def get(self, session_id: str) -> WorkSession: ...


class NoOpBenchmark(BenchmarkPort):
    """Fallback implementation for compositions that do not install benchmarking."""

    def record_run(self, comparison_id: str, result: ModelRunResult) -> None:
        self._runs[(comparison_id, result.model_id)] = result

    def compare(self, comparison_id: str, problem_id: str) -> ComparisonRun:
        return ComparisonRun(
            comparison_id,
            problem_id,
            tuple(value for (key, _), value in self._runs.items() if key == comparison_id),
        )

    def __init__(self) -> None:
        self._runs = {}


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


class MockTimetableProvider:
    """Deterministic provider for local flow demonstrations only."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def extract(self, problem: OriginalProblem) -> ExtractionProposal:
        if problem.prompt.strip() != MOCK_DEMO_PROMPT:
            return ExtractionProposal(
                str(uuid4()), problem.problem_id, self.model_id, TimetableData(),
                missing_information=(MissingInformation(
                    "MOCK_FIXTURE_ONLY",
                    "Mock mode only extracts the documented demo prompt. No entities were inferred; enter explicit timetable data in the draft editor.",
                ),),
                raw_response="Mock provider did not interpret this prompt.",
            )
        data = TimetableData(
            classes=(SchoolClass("class-1", "Clasa 1", 24), SchoolClass("class-2", "Clasa 2", 22)),
            teachers=(Teacher("teacher-1", "Profesor 1", ("subject-1",)), Teacher("teacher-2", "Profesor 2", ("subject-1",))),
            subjects=(Subject("subject-1", "Matematică"),),
            rooms=(Room("room-1", "Sala 1", 30), Room("room-2", "Sala 2", 30)),
            time_slots=tuple(TimeSlot(f"luni-{period}", Weekday.MONDAY, period) for period in range(1, 5)),
            requirements=(
                TeachingRequirement("req-1", "class-1", "subject-1", "teacher-1", 1),
                TeachingRequirement("req-2", "class-2", "subject-1", "teacher-2", 1),
            ),
        )
        return ExtractionProposal(
            str(uuid4()), problem.problem_id, self.model_id, data,
            raw_response="Deterministic response for the exact documented mock fixture.",
        )

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


class MockProviderFactory(AIProviderFactoryPort):
    MODEL_IDS = MOCK_MODEL_IDS

    def get_provider(self, model_id: str) -> MockTimetableProvider:
        if model_id not in self.MODEL_IDS:
            raise KeyError(f"Unknown model '{model_id}'.")
        return MockTimetableProvider(model_id)


class WorkflowService:
    """Facade for lifecycle operations; dependencies are supplied via ports."""

    def __init__(
        self,
        sessions: SessionRepository,
        providers: AIProviderFactoryPort,
        verifier: VerificationPort,
        benchmark_factory=None,
        *,
        execution: AIExecutionPort | None = None,
    ) -> None:
        self.sessions = sessions
        self.providers = providers
        self.verifier = verifier
        self.benchmark_factory = benchmark_factory
        self.execution = execution if execution is not None else InlineAIExecution()
        self.mock_demo_prompt = ""

    def create_session(self, prompt: str) -> WorkSession:
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty.")
        session_id = str(uuid4())
        providers = self.providers
        model_ids = tuple(getattr(providers, "MODEL_IDS", MockProviderFactory.MODEL_IDS))
        session = WorkSession(
            session_id, OriginalProblem(session_id, prompt),
            {
                model_id: ModelWorkspace(
                    model_id,
                    problem_management=TimetableProblemService(InMemoryProblemRepository()),
                )
                for model_id in model_ids
            },
            str(uuid4()),
            self.benchmark_factory(session_id) if self.benchmark_factory else NoOpBenchmark(),
        )
        self.sessions.create(session)
        logger.info("session.created session_id=%s", session_id)
        return session

    def extract(self, session_id: str, model_ids: list[str] | None = None) -> WorkSession:
        session = self.sessions.get(session_id)
        if model_ids is not None and (type(model_ids) is not list or any(type(item) is not str for item in model_ids)):
            raise ValueError("Choose a list of configured models.")
        targets = list(session.models) if not model_ids else model_ids.copy()
        if len(set(targets)) != len(targets):
            raise ValueError("Choose unique models.")
        # Validate the entire request and capacity before invoking any provider.
        workspaces = [self._workspace(session, model_id) for model_id in targets]
        pending = [workspace for workspace in workspaces if workspace.proposal is None]
        for workspace in pending:
            self._ensure_attempt_capacity(workspace)
        providers = {workspace.model_id: self.providers.get_provider(workspace.model_id) for workspace in pending}
        for workspace in workspaces:
            if workspace.proposal is None or not workspace._extraction_recorded or workspace.draft is None:
                workspace.transition(WorkflowStatus.EXTRACTING)
        outcomes = self.execution.execute("extract", {
            model_id: lambda provider=provider: provider.extract(session.problem)
            for model_id, provider in providers.items()
        }) if providers else {}
        # Only provider calls run in the executor. All state and benchmark writes stay serial.
        for workspace in workspaces:
            if workspace.status != WorkflowStatus.EXTRACTING:
                continue
            outcome = outcomes.get(workspace.model_id)
            if outcome is not None:
                proposal = outcome.result
                if outcome.failure is None and (
                    not isinstance(proposal, ExtractionProposal)
                    or proposal.model_id != workspace.model_id
                    or proposal.problem_id != session.problem.problem_id
                ):
                    outcome = self._invalid_outcome(outcome)
                self._record_attempt(workspace, "extract", outcome)
                workspace.extraction_duration_ms = outcome.duration_ms
                if outcome.failure is not None:
                    self._fail(session, workspace, outcome.failure, "extraction")
                    continue
                workspace.proposal = proposal
            try:
                assert workspace.proposal is not None and workspace.problem_management is not None
                if workspace.draft is None:
                    workspace.draft = workspace.problem_management.create_draft(workspace.proposal)
                if not workspace._extraction_recorded:
                    record_extraction = getattr(session.benchmark, "ensure_extraction_recorded", None) or getattr(session.benchmark, "record_extraction", None)
                    if record_extraction is not None:
                        record_extraction(session.comparison_id, workspace.proposal, workspace.extraction_duration_ms)
                    else:
                        session.benchmark.record_run(session.comparison_id, ModelRunResult(
                            model_id=workspace.model_id, proposal_id=workspace.proposal.proposal_id,
                        ))
                    workspace._extraction_recorded = True
                workspace.transition(WorkflowStatus.DRAFT)
                self._clear_failure(workspace)
            except Exception as error:
                self._fail(session, workspace, classify_ai_failure(error), "extraction")
        return session

    def edit_draft(self, session_id: str, model_id: str, data: TimetableData) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.draft is None or workspace.status not in (
            WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED, WorkflowStatus.COMPLETED, WorkflowStatus.FAILED,
        ):
            raise ValueError("Extract a draft before editing it.")
        assert workspace.problem_management is not None
        if workspace._pending_edit is not None:
            self._commit_pending_edit(session, workspace)
        operations = self._plan_draft_operations(workspace.draft.data, data)
        if not operations:
            return workspace
        workspace._pending_edit = _PendingDraftEdit(
            edit_id=str(uuid4()), operations=tuple(operations),
            revision_started=workspace.snapshot is None,
        )
        # Invalidate current results before touching either local port. Immutable
        # histories remain available even if a subsequent write needs replay.
        workspace.snapshot = None
        workspace.solution = None
        workspace.verification = None
        workspace.solving_duration_ms = None
        workspace.verification_duration_ms = None
        workspace._solution_recorded = False
        workspace._verification_recorded = False
        workspace.transition(WorkflowStatus.DRAFT)
        self._commit_pending_edit(session, workspace)
        logger.info("session.draft_edited session_id=%s model_id=%s revision=%s", session_id, model_id, workspace.draft.revision)
        return workspace

    def _commit_pending_edit(self, session: WorkSession, workspace: ModelWorkspace) -> None:
        pending = workspace._pending_edit
        assert pending is not None and workspace.problem_management is not None and workspace.draft is not None
        try:
            if not pending.revision_started:
                ensure_revision = getattr(session.benchmark, "ensure_revision_started", None)
                start_revision = getattr(session.benchmark, "start_revision", None)
                if ensure_revision is not None:
                    ensure_revision(session.comparison_id, workspace.model_id, pending.edit_id)
                elif start_revision is not None:
                    start_revision(session.comparison_id, workspace.model_id)
                pending.revision_started = True
            for index, operation in enumerate(pending.operations):
                if pending.applied_count <= index:
                    current = workspace.problem_management.get_draft(workspace.draft.draft_id)
                    if not self._operation_already_applied(current.data, operation):
                        workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)
                    pending.applied_count += 1
                    workspace.edit_count += 1
                    workspace.draft = workspace.problem_management.get_draft(workspace.draft.draft_id)
                if pending.recorded_count <= index:
                    record_edit = getattr(session.benchmark, "ensure_edit_recorded", None) or getattr(session.benchmark, "record_edit", None)
                    if record_edit is not None:
                        record_edit(session.comparison_id, workspace.model_id, operation)
                    pending.recorded_count += 1
            workspace._pending_edit = None
            if workspace.status != WorkflowStatus.DRAFT:
                workspace.transition(WorkflowStatus.DRAFT)
            self._clear_failure(workspace)
        except Exception as error:
            # A port may have persisted an operation before raising. Refresh the
            # public draft now; its planned operation is checked on explicit replay.
            workspace.draft = workspace.problem_management.get_draft(workspace.draft.draft_id)
            self._fail(session, workspace, classify_ai_failure(error), "draft_edit")
            raise

    def confirm(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.draft is None:
            raise ValueError("Extract a draft before confirming it.")
        if workspace._pending_edit is not None:
            raise ValueError("Retry the pending draft edit before confirming it.")
        if workspace.snapshot is not None:
            return workspace
        if workspace.status != WorkflowStatus.DRAFT:
            raise ValueError("Only a changed draft can be confirmed.")
        assert workspace.problem_management is not None
        workspace.snapshot = workspace.problem_management.confirm(workspace.draft.draft_id, session.problem.prompt)
        workspace.snapshots.append(workspace.snapshot)
        workspace.transition(WorkflowStatus.CONFIRMED)
        self._clear_failure(workspace)
        logger.info("session.confirmed session_id=%s model_id=%s version=%s", session_id, model_id, workspace.snapshot.version)
        return workspace

    def solve(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace._pending_edit is not None:
            raise ValueError("Retry the pending draft edit before solving it.")
        if workspace.status == WorkflowStatus.COMPLETED and workspace.solution is not None:
            return workspace
        if workspace.snapshot is None or workspace.status not in (WorkflowStatus.CONFIRMED, WorkflowStatus.FAILED):
            raise ValueError("Confirm the draft before solving.")
        if workspace.solution is None:
            self._ensure_attempt_capacity(workspace)
        if workspace.status == WorkflowStatus.FAILED:
            workspace.transition(WorkflowStatus.CONFIRMED)
        workspace.transition(WorkflowStatus.SOLVING)
        try:
            if workspace.solution is None:
                provider = self.providers.get_provider(model_id)
                snapshot = workspace.snapshot
                outcome = self.execution.execute("solve", {model_id: lambda: provider.solve(snapshot)})[model_id]
                solution = outcome.result
                if outcome.failure is None and (
                    not isinstance(solution, TimetableSolution)
                    or solution.model_id != model_id or solution.snapshot_id != snapshot.snapshot_id
                ):
                    outcome = self._invalid_outcome(outcome)
                self._record_attempt(workspace, "solve", outcome)
                workspace.solving_duration_ms = outcome.duration_ms
                if outcome.failure is not None:
                    self._fail(session, workspace, outcome.failure, "solve")
                    if outcome.exception is not None:
                        raise outcome.exception
                    raise AIProviderError(outcome.failure.code, retry_after_seconds=outcome.failure.retry_after_seconds)
                # Preserve paid output before any local operation can fail.
                workspace.solution = solution
                workspace.solutions.append(solution)
            if not workspace._solution_recorded:
                record_solution = getattr(session.benchmark, "ensure_solution_recorded", None) or getattr(session.benchmark, "record_solution", None)
                if record_solution is not None:
                    record_solution(session.comparison_id, workspace.solution, workspace.solving_duration_ms or 0)
                else:
                    session.benchmark.record_run(session.comparison_id, self._summary(workspace))
                workspace._solution_recorded = True
            workspace.transition(WorkflowStatus.COMPLETED)
            self._clear_failure(workspace)
        except Exception as error:
            if workspace.status != WorkflowStatus.FAILED:
                self._fail(session, workspace, classify_ai_failure(error), "solve")
            raise
        return workspace

    def verify(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.snapshot is None or workspace.solution is None or not workspace._solution_recorded:
            raise ValueError("Solve the confirmed draft before verification.")
        if workspace.verification is not None and workspace._verification_recorded:
            return workspace
        try:
            if workspace.verification is None:
                started = perf_counter()
                workspace.verification = self.verifier.verify(workspace.snapshot, workspace.solution)
                workspace.verification_duration_ms = round((perf_counter() - started) * 1000, 3)
                workspace.verifications.append(workspace.verification)
                workspace.verification_durations_ms.append(workspace.verification_duration_ms)
            result = workspace.verification
            duration_ms = workspace.verification_duration_ms
            record_verification = getattr(session.benchmark, "ensure_verification_recorded", None) or getattr(session.benchmark, "record_verification", None)
            if record_verification is not None:
                record_verification(session.comparison_id, model_id, result, duration_ms=duration_ms)
            elif isinstance(session.benchmark, NoOpBenchmark):
                session.benchmark.record_run(session.comparison_id, self._summary(workspace))
            workspace._verification_recorded = True
            if workspace.status == WorkflowStatus.FAILED:
                workspace.transition(WorkflowStatus.COMPLETED)
            self._clear_failure(workspace)
        except Exception as error:
            self._fail(session, workspace, classify_ai_failure(error), "verification")
            raise
        logger.info("session.verified session_id=%s model_id=%s status=%s", session_id, model_id, result.status.value)
        return workspace

    def compare(self, session_id: str) -> ComparisonRun:
        session = self.sessions.get(session_id)
        comparison = session.benchmark.compare(session.comparison_id, session.problem.problem_id)
        existing = {item.model_id: item for item in comparison.model_runs}
        # Merge every workspace into the benchmark's metrics, including failed and pending models.
        summaries = []
        for model_id, workspace in session.models.items():
            summary = self._summary(workspace)
            current = existing.get(model_id)
            if current is not None:
                summary = replace(current, **{
                    name: getattr(summary, name)
                    for name in ("proposal_id", "snapshot_id", "solution_id", "verification_status", "error", "edit_count", "solving_duration_ms")
                })
            summaries.append(summary)
        return ComparisonRun(comparison.comparison_id, comparison.problem_id, tuple(summaries))

    def comparison_report(self, session_id: str):
        """Detailed reporting is an optional capability beyond the shared benchmark port."""
        session = self.sessions.get(session_id)
        compare_runs = getattr(session.benchmark, "compare_runs", None)
        return compare_runs(session.comparison_id, session.problem.problem_id) if compare_runs is not None else None

    @staticmethod
    def _summary(workspace: ModelWorkspace) -> ModelRunResult:
        return ModelRunResult(
            model_id=workspace.model_id,
            proposal_id=workspace.proposal.proposal_id if workspace.proposal else None,
            snapshot_id=workspace.snapshot.snapshot_id if workspace.snapshot else None,
            solution_id=workspace.solution.solution_id if workspace.solution else None,
            verification_status=workspace.verification.status if workspace.verification else None,
            extraction_duration_ms=round(workspace.extraction_duration_ms) if workspace.extraction_duration_ms is not None else None,
            solving_duration_ms=round(workspace.solving_duration_ms) if workspace.solving_duration_ms is not None else None,
            edit_count=workspace.edit_count, error=workspace.error,
        )

    @staticmethod
    def _record_attempt(workspace: ModelWorkspace, operation: str, outcome: AICallOutcome) -> None:
        if not getattr(outcome, "attempted", True):
            return
        workspace.attempts.append(AIAttempt(
            attempt_id=str(uuid4()), operation=operation, model_id=workspace.model_id,
            snapshot_id=workspace.snapshot.snapshot_id if operation == "solve" and workspace.snapshot else None,
            duration_ms=outcome.duration_ms, failure=outcome.failure,
            invocation=outcome.invocation, raw_response=outcome.raw_response,
        ))

    @staticmethod
    def _ensure_attempt_capacity(workspace: ModelWorkspace) -> None:
        if len(workspace.attempts) >= 100:
            raise ValueError("This model has reached the session limit of 100 AI attempts. Create a new session.")

    @staticmethod
    def _invalid_outcome(outcome: AICallOutcome) -> AICallOutcome:
        message = "The AI response does not match the requested workflow."
        return replace(
            outcome, failure=AICallFailure("invalid_response", message),
            exception=AIResponseError("invalid_response", "$", message),
        )

    @staticmethod
    def _clear_failure(workspace: ModelWorkspace) -> None:
        workspace.error = None
        workspace.failure = None

    @staticmethod
    def _fail(session: WorkSession, workspace: ModelWorkspace, failure: AICallFailure, operation: str) -> None:
        workspace.transition(WorkflowStatus.FAILED)
        workspace.failure = failure
        workspace.error = failure.message
        logger.error("session.%s_failed session_id=%s model_id=%s code=%s", operation, session.session_id, workspace.model_id, failure.code)

    @staticmethod
    def _workspace(session: WorkSession, model_id: str) -> ModelWorkspace:
        try:
            return session.models[model_id]
        except KeyError as error:
            raise KeyError(f"Unknown model '{model_id}'.") from error

    _EDIT_COLLECTIONS = {
        "class": ("classes", "class_id"),
        "teacher": ("teachers", "teacher_id"),
        "subject": ("subjects", "subject_id"),
        "room": ("rooms", "room_id"),
        "time_slot": ("time_slots", "slot_id"),
        "requirement": ("requirements", "requirement_id"),
        "constraint": ("constraints", "constraint_id"),
    }

    @staticmethod
    def _encode_entity(entity) -> str:
        def normalize(value):
            if hasattr(value, "value"):
                return value.value
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [normalize(item) for item in value]
            return value
        return json.dumps(normalize(asdict(entity)))

    @classmethod
    def _operation_already_applied(cls, data: TimetableData, operation: EditOperation) -> bool:
        collection, id_field = cls._EDIT_COLLECTIONS[operation.entity_type]
        entity = next((item for item in getattr(data, collection) if getattr(item, id_field) == operation.entity_id), None)
        if operation.action == "remove":
            return entity is None
        return entity is not None and cls._encode_entity(entity) == operation.after_json

    @classmethod
    def _plan_draft_operations(cls, current: TimetableData, data: TimetableData) -> list[EditOperation]:
        """Create stable, replayable commands without mutating either local port."""
        operations: list[EditOperation] = []
        for entity_type, (collection, id_field) in cls._EDIT_COLLECTIONS.items():
            before_by_id = {getattr(item, id_field): item for item in getattr(current, collection)}
            after_by_id = {getattr(item, id_field): item for item in getattr(data, collection)}
            for entity_id, old in before_by_id.items():
                if entity_id not in after_by_id:
                    operations.append(EditOperation(str(uuid4()), entity_type, entity_id, "remove", cls._encode_entity(old), None))
                elif old != after_by_id[entity_id]:
                    operations.append(EditOperation(str(uuid4()), entity_type, entity_id, "update", cls._encode_entity(old), cls._encode_entity(after_by_id[entity_id])))
            for entity_id, new in after_by_id.items():
                if entity_id not in before_by_id:
                    operations.append(EditOperation(str(uuid4()), entity_type, entity_id, "add", None, cls._encode_entity(new)))
        return operations


def build_default_service() -> WorkflowService:
    service = WorkflowService(
        InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier(),
    )
    service.mock_demo_prompt = MOCK_DEMO_PROMPT
    return service
