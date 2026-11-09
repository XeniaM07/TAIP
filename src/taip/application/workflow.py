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
        WorkflowStatus.FAILED: {WorkflowStatus.EXTRACTING, WorkflowStatus.CONFIRMED, WorkflowStatus.FAILED},
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
    ) -> None:
        self.sessions = sessions
        self.providers = providers
        self.verifier = verifier
        self.benchmark_factory = benchmark_factory
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
        targets = model_ids or list(session.models)
        for model_id in targets:
            workspace = self._workspace(session, model_id)
            if workspace.proposal is not None:
                continue
            workspace.transition(WorkflowStatus.EXTRACTING)
            started = perf_counter()
            try:
                proposal = self.providers.get_provider(model_id).extract(session.problem)
                assert workspace.problem_management is not None
                workspace.proposal = proposal
                workspace.draft = workspace.problem_management.create_draft(proposal)
                record_extraction = getattr(session.benchmark, "record_extraction", None)
                if record_extraction is not None:
                    record_extraction(
                        session.comparison_id, proposal,
                        round((perf_counter() - started) * 1000, 3),
                    )
                else:
                    session.benchmark.record_run(
                        session.comparison_id,
                        ModelRunResult(model_id=model_id, proposal_id=proposal.proposal_id),
                    )
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
        if workspace.draft is None or workspace.status not in (WorkflowStatus.DRAFT, WorkflowStatus.CONFIRMED):
            raise ValueError("Extract a draft before editing it.")
        assert workspace.problem_management is not None
        operations = self._replace_draft_data(workspace, data)
        for operation in operations:
            record_edit = getattr(session.benchmark, "record_edit", None)
            if record_edit is not None:
                record_edit(session.comparison_id, model_id, operation)
        workspace.draft = workspace.problem_management.get_draft(workspace.draft.draft_id)
        workspace.solution = None
        workspace.verification = None
        workspace.transition(WorkflowStatus.DRAFT)
        workspace.edit_count += len(operations)
        logger.info("session.draft_edited session_id=%s model_id=%s revision=%s", session_id, model_id, workspace.draft.revision)
        return workspace

    def confirm(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        if workspace.draft is None:
            raise ValueError("Extract a draft before confirming it.")
        if workspace.status == WorkflowStatus.CONFIRMED and workspace.snapshot is not None:
            return workspace
        if workspace.status != WorkflowStatus.DRAFT:
            raise ValueError("Only a changed draft can be confirmed.")
        assert workspace.problem_management is not None
        workspace.snapshot = workspace.problem_management.confirm(
            workspace.draft.draft_id, session.problem.prompt
        )
        workspace.snapshots.append(workspace.snapshot)
        workspace.solution = None
        workspace.verification = None
        workspace.transition(WorkflowStatus.CONFIRMED)
        logger.info("session.confirmed session_id=%s model_id=%s version=%s", session_id, model_id, workspace.snapshot.version)
        return workspace

    def solve(self, session_id: str, model_id: str) -> ModelWorkspace:
        session = self.sessions.get(session_id)
        workspace = self._workspace(session, model_id)
        # An explicit retry reuses the already confirmed immutable snapshot.
        # Provider cooldown/admission still applies; no automatic retry occurs.
        if workspace.status == WorkflowStatus.FAILED and workspace.snapshot is not None:
            workspace.transition(WorkflowStatus.CONFIRMED)
        if workspace.snapshot is None or workspace.status != WorkflowStatus.CONFIRMED:
            raise ValueError("Confirm the draft before solving.")
        workspace.transition(WorkflowStatus.SOLVING)
        started = perf_counter()
        try:
            solution = self.providers.get_provider(model_id).solve(workspace.snapshot)
            if solution.model_id != model_id or solution.snapshot_id != workspace.snapshot.snapshot_id:
                raise ValueError("Provider returned a solution for a different model or snapshot.")
            record_solution = getattr(session.benchmark, "record_solution", None)
            if record_solution is not None:
                record_solution(
                    session.comparison_id, solution,
                    round((perf_counter() - started) * 1000, 3),
                )
            else:
                session.benchmark.record_run(
                    session.comparison_id,
                    ModelRunResult(
                        model_id=model_id,
                        proposal_id=workspace.proposal.proposal_id if workspace.proposal else None,
                        snapshot_id=solution.snapshot_id,
                        solution_id=solution.solution_id,
                        extraction_duration_ms=round(workspace.extraction_duration_ms) if workspace.extraction_duration_ms is not None else None,
                        solving_duration_ms=round((perf_counter() - started) * 1000),
                        edit_count=workspace.edit_count,
                    ),
                )
            workspace.solution = solution
            workspace.solutions.append(solution)
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
        if workspace.verification is not None:
            return workspace
        started = perf_counter()
        result = self.verifier.verify(workspace.snapshot, workspace.solution)
        duration_ms = round((perf_counter() - started) * 1000, 3)
        record_verification = getattr(session.benchmark, "record_verification", None)
        if record_verification is not None:
            record_verification(
                session.comparison_id, model_id, result, duration_ms=duration_ms
            )
        elif isinstance(session.benchmark, NoOpBenchmark):
            session.benchmark.record_run(
                session.comparison_id,
                ModelRunResult(
                    model_id=model_id,
                    proposal_id=workspace.proposal.proposal_id if workspace.proposal else None,
                    snapshot_id=result.snapshot_id,
                    solution_id=result.solution_id,
                    verification_status=result.status,
                    extraction_duration_ms=round(workspace.extraction_duration_ms) if workspace.extraction_duration_ms is not None else None,
                    solving_duration_ms=round(workspace.solving_duration_ms) if workspace.solving_duration_ms is not None else None,
                    edit_count=workspace.edit_count,
                ),
            )
        workspace.verification = result
        workspace.verifications.append(result)
        logger.info("session.verified session_id=%s model_id=%s status=%s", session_id, model_id, result.status.value)
        return workspace

    def compare(self, session_id: str) -> ComparisonRun:
        session = self.sessions.get(session_id)
        if getattr(session.benchmark, "compare_runs", None) is not None:
            return session.benchmark.compare(session.comparison_id, session.problem.problem_id)
        if getattr(session.benchmark, "record_extraction", None) is None:
            comparison = session.benchmark.compare(session.comparison_id, session.problem.problem_id)
            existing = {item.model_id: item for item in comparison.model_runs}
            summaries = []
            for model_id, workspace in session.models.items():
                current = existing.get(model_id)
                if current is not None and current.proposal_id is not None:
                    summaries.append(current)
                elif workspace.proposal is not None:
                    summaries.append(ModelRunResult(
                        model_id=model_id,
                        proposal_id=workspace.proposal.proposal_id,
                        snapshot_id=workspace.verification.snapshot_id if workspace.verification else workspace.snapshot.snapshot_id if workspace.snapshot else None,
                        solution_id=workspace.verification.solution_id if workspace.verification else workspace.solution.solution_id if workspace.solution else None,
                        verification_status=workspace.verification.status if workspace.verification else None,
                        extraction_duration_ms=round(workspace.extraction_duration_ms) if workspace.extraction_duration_ms is not None else None,
                        solving_duration_ms=round(workspace.solving_duration_ms) if workspace.solving_duration_ms is not None else None,
                        edit_count=workspace.edit_count,
                        error=workspace.error,
                    ))
                else:
                    summaries.append(ModelRunResult(model_id=model_id))
            return ComparisonRun(comparison.comparison_id, comparison.problem_id, tuple(summaries))
        comparison = session.benchmark.compare(session.comparison_id, session.problem.problem_id)
        existing = {item.model_id: item for item in comparison.model_runs}
        summaries = tuple(
            existing.get(model_id)
            or ModelRunResult(
                model_id=model_id,
                proposal_id=workspace.proposal.proposal_id if workspace.proposal else None,
                snapshot_id=workspace.verification.snapshot_id if workspace.verification else workspace.solution.snapshot_id if workspace.solution else None,
                solution_id=workspace.verification.solution_id if workspace.verification else workspace.solution.solution_id if workspace.solution else None,
                verification_status=workspace.verification.status if workspace.verification else None,
                extraction_duration_ms=round(workspace.extraction_duration_ms) if workspace.extraction_duration_ms is not None else None,
                solving_duration_ms=round(workspace.solving_duration_ms) if workspace.solving_duration_ms is not None else None,
                edit_count=workspace.edit_count,
                error=workspace.error,
            )
            for model_id, workspace in session.models.items()
        )
        return ComparisonRun(comparison.comparison_id, comparison.problem_id, summaries)

    def comparison_report(self, session_id: str):
        """Detailed reporting is an optional capability beyond the shared benchmark port."""
        session = self.sessions.get(session_id)
        compare_runs = getattr(session.benchmark, "compare_runs", None)
        if compare_runs is None:
            return None
        if compare_runs is None:
            return None
        return compare_runs(session.comparison_id, session.problem.problem_id)

    @staticmethod
    def _workspace(session: WorkSession, model_id: str) -> ModelWorkspace:
        try:
            return session.models[model_id]
        except KeyError as error:
            raise KeyError(f"Unknown model '{model_id}'.") from error

    @staticmethod
    def _replace_draft_data(workspace: ModelWorkspace, data: TimetableData) -> list[EditOperation]:
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

        operations: list[EditOperation] = []
        for entity_type, (before, after, id_field) in collections.items():
            before_by_id = {getattr(item, id_field): item for item in before}
            after_by_id = {getattr(item, id_field): item for item in after}
            for entity_id, old in before_by_id.items():
                if entity_id not in after_by_id:
                    operation = EditOperation(str(uuid4()), entity_type, entity_id, "remove", encoded(old), None)
                    workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)
                    operations.append(operation)
                elif old != after_by_id[entity_id]:
                    operation = EditOperation(str(uuid4()), entity_type, entity_id, "update", encoded(old), encoded(after_by_id[entity_id]))
                    workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)
                    operations.append(operation)
            for entity_id, new in after_by_id.items():
                if entity_id not in before_by_id:
                    operation = EditOperation(str(uuid4()), entity_type, entity_id, "add", None, encoded(new))
                    workspace.problem_management.apply_edit(workspace.draft.draft_id, operation)
                    operations.append(operation)
        return operations


def build_default_service() -> WorkflowService:
    service = WorkflowService(
        InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier(),
    )
    service.mock_demo_prompt = MOCK_DEMO_PROMPT
    return service
