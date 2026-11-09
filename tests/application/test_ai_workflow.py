"""Paid provider work is durable across serial local workflow operations."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from taip.ai_integration.errors import AIProviderError, AIResponseError
from taip.common.ai_execution import AICallFailure, AICallOutcome
from taip.application.workflow import (
    MOCK_DEMO_PROMPT, MOCK_MODEL_IDS, InMemorySessionRepository,
    MockTimetableProvider, WorkflowService, WorkflowStatus,
)
from taip.benchmark.timetable_service import TimetableBenchmarkService
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


class AIWorkflowTests(TestCase):
    def make_service(self, *, benchmark=False, execution=None):
        providers = {model_id: Mock(wraps=MockTimetableProvider(model_id)) for model_id in MOCK_MODEL_IDS}
        factory = SimpleNamespace(MODEL_IDS=MOCK_MODEL_IDS, get_provider=providers.__getitem__)
        kwargs = {"execution": execution} if execution is not None else {}
        service = WorkflowService(
            InMemorySessionRepository(), factory, TimetableVerifier(),
            TimetableBenchmarkService if benchmark else None, **kwargs,
        )
        return service, service.create_session(MOCK_DEMO_PROMPT), providers

    def completed(self, *, benchmark=False):
        service, session, providers = self.make_service(benchmark=benchmark)
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        service.confirm(session.session_id, model_id)
        service.solve(session.session_id, model_id)
        service.verify(session.session_id, model_id)
        return service, session, providers, session.models[model_id]

    def test_whole_selection_is_validated_before_any_provider_call(self):
        for selection in ([MOCK_MODEL_IDS[0], "unknown"], [MOCK_MODEL_IDS[0]] * 2, "invalid", [False]):
            with self.subTest(selection=selection):
                service, session, providers = self.make_service()
                with self.assertRaises((KeyError, ValueError)):
                    service.extract(session.session_id, selection)
                self.assertTrue(all(provider.extract.call_count == 0 for provider in providers.values()))

    def test_paid_extraction_is_reused_after_local_draft_failure(self):
        service, session, providers = self.make_service()
        model_id = MOCK_MODEL_IDS[0]
        workspace = session.models[model_id]
        original = workspace.problem_management.create_draft
        workspace.problem_management.create_draft = Mock(side_effect=[RuntimeError("private-secret"), None])
        service.extract(session.session_id, [model_id])
        self.assertEqual(workspace.status, WorkflowStatus.FAILED)
        workspace.problem_management.create_draft = original
        service.extract(session.session_id, [model_id])
        self.assertEqual(workspace.status, WorkflowStatus.DRAFT)
        self.assertIsNotNone(workspace.draft)
        self.assertEqual(providers[model_id].extract.call_count, 1)
        self.assertEqual(len(workspace.attempts), 1)

    def test_unchanged_completed_flow_and_noop_edit_make_no_new_calls(self):
        service, session, providers, workspace = self.completed()
        snapshot, solution, verification = workspace.snapshot, workspace.solution, workspace.verification
        service.edit_draft(session.session_id, workspace.model_id, workspace.draft.data)
        service.confirm(session.session_id, workspace.model_id)
        service.solve(session.session_id, workspace.model_id)
        service.verify(session.session_id, workspace.model_id)
        self.assertIs(workspace.snapshot, snapshot)
        self.assertIs(workspace.solution, solution)
        self.assertIs(workspace.verification, verification)
        self.assertEqual(workspace.status, WorkflowStatus.COMPLETED)
        self.assertEqual(providers[workspace.model_id].solve.call_count, 1)
        self.assertEqual(len(workspace.attempts), 2)

    def test_changed_completed_draft_retains_history_and_benchmark_revision(self):
        service, session, providers, workspace = self.completed(benchmark=True)
        old_snapshot, old_solution = workspace.snapshot, workspace.solution
        updated = replace(workspace.draft.data, rooms=(replace(workspace.draft.data.rooms[0], name="Renamed"),) + workspace.draft.data.rooms[1:])
        service.edit_draft(session.session_id, workspace.model_id, updated)
        self.assertIsNone(workspace.snapshot)
        self.assertIsNone(workspace.solution)
        self.assertEqual(workspace.status, WorkflowStatus.DRAFT)
        service.confirm(session.session_id, workspace.model_id)
        service.solve(session.session_id, workspace.model_id)
        service.verify(session.session_id, workspace.model_id)
        self.assertEqual(workspace.snapshots, [old_snapshot, workspace.snapshot])
        self.assertEqual(workspace.solutions, [old_solution, workspace.solution])
        archived = session.benchmark.repository.list_run_history(session.session_id, session.comparison_id, workspace.model_id)
        self.assertEqual(len(archived), 1)
        self.assertIs(archived[0].solution, old_solution)
        row = service.compare(session.session_id).model_runs[0]
        self.assertEqual(row.solution_id, workspace.solution.solution_id)
        self.assertEqual(len(service.compare(session.session_id).model_runs), 3)

    def test_provider_failure_retry_preserves_original_exception_snapshot_and_attempt(self):
        service, session, providers = self.make_service()
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        workspace = service.confirm(session.session_id, model_id)
        snapshot = workspace.snapshot
        error = AIProviderError("rate_limit", retry_after_seconds=2)
        providers[model_id].solve.side_effect = error
        with self.assertRaises(AIProviderError) as caught:
            service.solve(session.session_id, model_id)
        self.assertIs(caught.exception, error)
        first_attempt = workspace.attempts[-1]
        self.assertEqual(first_attempt.failure.code, "rate_limit")
        self.assertEqual(workspace.failure.retry_after_seconds, 2)
        providers[model_id].solve.side_effect = None
        service.solve(session.session_id, model_id)
        self.assertIs(workspace.snapshot, snapshot)
        self.assertIs(workspace.attempts[-2], first_attempt)
        self.assertIsNone(workspace.failure)

    def test_paid_solution_is_reused_after_local_benchmark_failure(self):
        service, session, providers = self.make_service(benchmark=True)
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        workspace = service.confirm(session.session_id, model_id)
        original = session.benchmark.record_solution
        session.benchmark.record_solution = Mock(side_effect=RuntimeError("private-secret"))
        with self.assertRaises(RuntimeError):
            service.solve(session.session_id, model_id)
        paid_solution = workspace.solution
        session.benchmark.record_solution = original
        service.solve(session.session_id, model_id)
        self.assertIs(workspace.solution, paid_solution)
        self.assertEqual(providers[model_id].solve.call_count, 1)
        self.assertEqual(len(workspace.solutions), 1)

    def test_unexpected_exception_text_never_enters_public_state_or_logs(self):
        service, session, providers = self.make_service()
        model_id = MOCK_MODEL_IDS[0]
        providers[model_id].extract.side_effect = RuntimeError("private-secret")
        with self.assertLogs("taip.application.workflow", "ERROR") as captured:
            service.extract(session.session_id, [model_id])
        workspace = session.models[model_id]
        self.assertNotIn("private-secret", workspace.error)
        self.assertNotIn("private-secret", str(workspace.failure))
        self.assertNotIn("private-secret", "\n".join(captured.output))
        self.assertEqual(workspace.failure.code, "unexpected_error")

    def test_mismatched_provider_proposal_is_rejected_before_local_state(self):
        service, session, providers = self.make_service()
        model_id = MOCK_MODEL_IDS[0]
        proposal = MockTimetableProvider(model_id).extract(session.problem)
        providers[model_id].extract.return_value = replace(proposal, problem_id="other")
        service.extract(session.session_id, [model_id])
        self.assertEqual(session.models[model_id].status, WorkflowStatus.FAILED)
        self.assertIsNone(session.models[model_id].proposal)
        self.assertEqual(session.models[model_id].failure.code, "invalid_response")

    def test_partial_comparison_includes_pending_and_failed_workspaces(self):
        service, session, providers = self.make_service(benchmark=True)
        providers[MOCK_MODEL_IDS[0]].extract.side_effect = AIProviderError("timeout")
        service.extract(session.session_id, [MOCK_MODEL_IDS[0], MOCK_MODEL_IDS[1]])
        rows = service.compare(session.session_id).model_runs
        self.assertEqual([row.model_id for row in rows], list(MOCK_MODEL_IDS))
        self.assertIsNotNone(rows[0].error)
        self.assertIsNotNone(rows[1].proposal_id)
        self.assertIsNone(rows[2].proposal_id)

    def test_attempt_limit_is_checked_for_whole_batch_before_paid_calls(self):
        service, session, providers = self.make_service()
        session.models[MOCK_MODEL_IDS[1]].attempts = [None] * 100
        with self.assertRaisesRegex(ValueError, "100"):
            service.extract(session.session_id, list(MOCK_MODEL_IDS))
        self.assertTrue(all(provider.extract.call_count == 0 for provider in providers.values()))

    def test_coordinator_rejection_does_not_create_a_provider_attempt(self):
        failure = AICallFailure("coordinator_busy", "AI execution is busy.", True, 1)
        execution = SimpleNamespace(execute=lambda operation, calls: {
            name: AICallOutcome(failure=failure, attempted=False) for name in calls
        })
        service, session, providers = self.make_service(execution=execution)
        service.extract(session.session_id)
        self.assertTrue(all(workspace.status == WorkflowStatus.FAILED and not workspace.attempts for workspace in session.models.values()))
        self.assertTrue(all(provider.extract.call_count == 0 for provider in providers.values()))

    def test_all_provider_calls_finish_before_serial_draft_commits(self):
        service, session, providers = self.make_service()
        completed = []
        for model_id, provider in providers.items():
            provider.extract.side_effect = lambda problem, model_id=model_id: (
                completed.append(model_id), MockTimetableProvider(model_id).extract(problem)
            )[1]
        for workspace in session.models.values():
            original = workspace.problem_management.create_draft
            def create(proposal, original=original):
                self.assertEqual(completed, list(MOCK_MODEL_IDS))
                return original(proposal)
            workspace.problem_management.create_draft = create
        service.extract(session.session_id)
        self.assertTrue(all(workspace.status == WorkflowStatus.DRAFT for workspace in session.models.values()))

    def test_extraction_commit_replay_after_benchmark_writes_then_raises(self):
        service, session, providers = self.make_service(benchmark=True)
        model_id = MOCK_MODEL_IDS[0]
        original = session.benchmark.record_extraction
        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private-secret")
        session.benchmark.record_extraction = broken
        service.extract(session.session_id, [model_id])
        workspace = session.models[model_id]
        draft = workspace.draft
        self.assertEqual(workspace.status, WorkflowStatus.FAILED)
        service.extract(session.session_id, [model_id])
        self.assertEqual(workspace.status, WorkflowStatus.DRAFT)
        self.assertIs(workspace.draft, draft)
        self.assertEqual(providers[model_id].extract.call_count, 1)

    def test_solve_commit_replay_after_benchmark_writes_then_raises(self):
        service, session, providers = self.make_service(benchmark=True)
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        workspace = service.confirm(session.session_id, model_id)
        original = session.benchmark.record_solution
        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private-secret")
        session.benchmark.record_solution = broken
        with self.assertRaises(RuntimeError):
            service.solve(session.session_id, model_id)
        solution = workspace.solution
        service.solve(session.session_id, model_id)
        self.assertEqual(workspace.status, WorkflowStatus.COMPLETED)
        self.assertIs(workspace.solution, solution)
        self.assertEqual(providers[model_id].solve.call_count, 1)

    def test_mismatched_solution_raises_safe_domain_error_and_can_be_retried(self):
        service, session, providers = self.make_service()
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        workspace = service.confirm(session.session_id, model_id)
        wrong = replace(MockTimetableProvider(model_id).solve(workspace.snapshot), snapshot_id="other")
        providers[model_id].solve.return_value = wrong
        with self.assertRaises(AIResponseError):
            service.solve(session.session_id, model_id)
        self.assertEqual(workspace.failure.code, "invalid_response")
        self.assertIsNone(workspace.solution)

    def test_changed_draft_clears_current_comparison_solve_duration(self):
        service, session, providers, workspace = self.completed()
        updated = replace(workspace.draft.data, rooms=(replace(workspace.draft.data.rooms[0], name="Renamed"),) + workspace.draft.data.rooms[1:])
        service.edit_draft(session.session_id, workspace.model_id, updated)
        row = service.compare(session.session_id).model_runs[0]
        self.assertIsNone(row.solving_duration_ms)
        self.assertIsNone(row.solution_id)

    def test_verification_local_commit_retry_keeps_first_verifier_result(self):
        service, session, providers = self.make_service(benchmark=True)
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        workspace = service.confirm(session.session_id, model_id)
        service.solve(session.session_id, model_id)
        service.verifier = Mock(wraps=service.verifier)
        original = session.benchmark.record_verification
        session.benchmark.record_verification = Mock(side_effect=RuntimeError("private-secret"))
        with self.assertRaises(RuntimeError):
            service.verify(session.session_id, model_id)
        first_result = workspace.verification
        self.assertIsNotNone(first_result)
        session.benchmark.record_verification = original
        service.verify(session.session_id, model_id)
        self.assertIs(workspace.verification, first_result)
        self.assertEqual(service.verifier.verify.call_count, 1)
        self.assertEqual(len(workspace.verifications), 1)

    def test_edit_commit_resumes_after_benchmark_failure_before_or_after_write(self):
        for stage in ("start_revision", "record_edit"):
            for after_write in (False, True):
                with self.subTest(stage=stage, after_write=after_write):
                    service, session, providers, workspace = self.completed(benchmark=True)
                    model_id = workspace.model_id
                    original_snapshot, original_solution = workspace.snapshot, workspace.solution
                    updated = replace(workspace.draft.data, rooms=tuple(
                        replace(room, name=room.name + " renamed") for room in workspace.draft.data.rooms
                    ))
                    original = getattr(session.benchmark, stage)
                    called = False
                    def broken(*args, **kwargs):
                        nonlocal called
                        if called:
                            return original(*args, **kwargs)
                        called = True
                        if after_write:
                            original(*args, **kwargs)
                        raise RuntimeError("private-local-error")
                    setattr(session.benchmark, stage, broken)
                    with self.assertRaises(RuntimeError):
                        service.edit_draft(session.session_id, model_id, updated)
                    self.assertIsNone(workspace.snapshot)
                    self.assertIsNone(workspace.solution)
                    with self.assertRaises(ValueError):
                        service.solve(session.session_id, model_id)
                    with self.assertRaises(ValueError):
                        service.confirm(session.session_id, model_id)
                    service.edit_draft(session.session_id, model_id, updated)
                    self.assertEqual(workspace.status, WorkflowStatus.DRAFT)
                    self.assertEqual(workspace.draft.data, updated)
                    self.assertEqual(workspace.draft.revision, 3)
                    self.assertEqual(workspace.edit_count, 2)
                    self.assertEqual(workspace.snapshots, [original_snapshot])
                    self.assertEqual(workspace.solutions, [original_solution])
                    archived = session.benchmark.repository.list_run_history(session.session_id, session.comparison_id, model_id)
                    self.assertEqual(len(archived), 1)
                    self.assertIs(archived[0].solution, original_solution)
                    current = session.benchmark.repository.get_run(session.session_id, session.comparison_id, model_id)
                    self.assertEqual(len(current.edits), 2)
                    self.assertEqual(providers[model_id].extract.call_count, 1)
                    self.assertEqual(providers[model_id].solve.call_count, 1)

    def test_edit_commit_resumes_when_problem_port_raises_after_applying_operation(self):
        service, session, providers, workspace = self.completed(benchmark=True)
        updated = replace(workspace.draft.data, rooms=(replace(workspace.draft.data.rooms[0], name="Renamed"),) + workspace.draft.data.rooms[1:])
        original = workspace.problem_management.apply_edit
        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private-local-error")
        workspace.problem_management.apply_edit = broken
        with self.assertRaises(RuntimeError):
            service.edit_draft(session.session_id, workspace.model_id, updated)
        workspace.problem_management.apply_edit = original
        service.edit_draft(session.session_id, workspace.model_id, updated)
        self.assertEqual(workspace.draft.data, updated)
        self.assertEqual(workspace.draft.revision, 2)
        self.assertEqual(workspace.edit_count, 1)

    def test_verification_commit_retry_preserves_original_duration_and_history(self):
        service, session, providers = self.make_service(benchmark=True)
        model_id = MOCK_MODEL_IDS[0]
        service.extract(session.session_id, [model_id])
        workspace = service.confirm(session.session_id, model_id)
        service.solve(session.session_id, model_id)
        original = session.benchmark.record_verification
        session.benchmark.record_verification = Mock(side_effect=RuntimeError("private-local-error"))
        with patch("taip.application.workflow.perf_counter", side_effect=[1, 1.125, 9, 9]):
            with self.assertRaises(RuntimeError):
                service.verify(session.session_id, model_id)
            session.benchmark.record_verification = original
            service.verify(session.session_id, model_id)
        record = session.benchmark.repository.get_run(session.session_id, session.comparison_id, model_id)
        self.assertEqual(record.verification_duration_ms, 125)
        self.assertEqual(workspace.verification_duration_ms, 125)
        self.assertEqual(workspace.verification_durations_ms, [125])
        updated = replace(workspace.draft.data, rooms=(replace(workspace.draft.data.rooms[0], name="Renamed"),) + workspace.draft.data.rooms[1:])
        service.edit_draft(session.session_id, model_id, updated)
        self.assertIsNone(workspace.verification_duration_ms)
        self.assertEqual(workspace.verification_durations_ms, [125])
        archived = session.benchmark.repository.list_run_history(session.session_id, session.comparison_id, model_id)
        self.assertEqual(archived[0].verification_duration_ms, 125)
