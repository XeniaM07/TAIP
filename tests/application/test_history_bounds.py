"""Large, repeated revisions stay readable and reject excess work before writes."""

from dataclasses import asdict, replace
from datetime import timedelta
import json
from unittest.mock import Mock
import unittest

import httpx

from taip.application.api import create_app
from taip.application.workflow import (
    MOCK_DEMO_PROMPT, MOCK_MODEL_IDS, InMemorySessionRepository,
    MockProviderFactory, WorkflowService,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from taip.common.timetable_contracts import (
    ConstraintViolation, LessonAssignment, TimetableSolution,
    TimetableVerificationResult, VerificationStatus,
)
from taip.common.ai_execution import AICallOutcome


class WorkflowHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = WorkflowService(InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.service)), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.session = self.service.create_session(MOCK_DEMO_PROMPT)
        self.service.extract(self.session.session_id, [MOCK_MODEL_IDS[0]])
        self.workspace = self.session.models[MOCK_MODEL_IDS[0]]
        self.base = f"/api/sessions/{self.session.session_id}"
        self.model_base = f"{self.base}/models/{self.workspace.model_id}"

    async def test_hundred_large_revisions_remain_readable_and_history_is_paginated(self):
        first_id = None
        for index in range(100):
            data = self.workspace.draft.data
            desired = replace(data, rooms=(replace(data.rooms[0], name=str(index) + "x" * 58000),) + data.rooms[1:])
            self.service.edit_draft(self.session.session_id, self.workspace.model_id, desired)
            self.service.confirm(self.session.session_id, self.workspace.model_id)
            first_id = first_id or self.workspace.snapshot.snapshot_id

        state = await self.client.get(self.base)
        self.assertEqual(state.status_code, 200)
        self.assertLess(len(state.content), 512 * 1024)
        public = state.json()["models"][self.workspace.model_id]
        self.assertEqual(public["history_counts"]["snapshots"], 100)
        self.assertNotIn("snapshots", public)
        self.assertEqual(public["snapshot"]["snapshot_id"], self.workspace.snapshot.snapshot_id)
        for offset, expected_id in ((0, first_id), (99, self.workspace.snapshot.snapshot_id)):
            page = await self.client.get(f"{self.model_base}/history", params={"kind": "snapshots", "offset": offset, "limit": 1})
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(page.json()["items"][0]["snapshot_id"], expected_id)
            self.assertEqual(page.json()["total"], 100)
            self.assertLess(len(page.content), 128 * 1024)
        before = self.workspace.draft
        snapshot = self.workspace.snapshot
        events = len(self.workspace.problem_management.audit_log.events)
        changed = replace(before.data, rooms=(replace(before.data.rooms[0], name="budget overflow"),) + before.data.rooms[1:])
        with self.assertRaisesRegex(ValueError, "[Hh]istory|[Ll]imit"):
            self.service.edit_draft(self.session.session_id, self.workspace.model_id, changed)
        self.assertIs(self.workspace.draft, before)
        self.assertIs(self.workspace.snapshot, snapshot)
        self.assertEqual(len(self.workspace.problem_management.audit_log.events), events)
        self.assertEqual((await self.client.get(self.base)).status_code, 200)

    async def test_history_query_validation_and_model_isolation(self):
        self.service.confirm(self.session.session_id, self.workspace.model_id)
        for params in ({"kind": "unknown"}, {"kind": "snapshots", "offset": -1}, {"kind": "snapshots", "limit": 0}, {"kind": "snapshots", "limit": 11}, {"kind": "snapshots", "offset": "bad"}):
            with self.subTest(params=params):
                response = await self.client.get(f"{self.model_base}/history", params=params)
                self.assertIn(response.status_code, (400, 422))
        other = await self.client.get(f"{self.base}/models/{MOCK_MODEL_IDS[1]}/history", params={"kind": "snapshots"})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json()["items"], [])
        unknown = await self.client.get(f"{self.base}/models/unknown/history", params={"kind": "snapshots"})
        self.assertEqual(unknown.status_code, 404)

    async def test_original_raw_proposal_is_retrievable_without_inflating_session(self):
        raw = "private original response\n" * 40000
        self.workspace.proposal = replace(self.workspace.proposal, raw_response=raw)
        state = await self.client.get(self.base)
        self.assertFalse("raw_response" in state.json()["models"][self.workspace.model_id]["proposal"])
        self.assertLess(len(state.content), 32 * 1024)
        for offset in (0, len(raw) - 5):
            response = await self.client.get(f"{self.model_base}/proposal/raw", params={"offset": offset, "limit": 5})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["text"], raw[offset:offset + 5])
            self.assertEqual(response.json()["total"], len(raw))

    async def test_history_attempts_exclude_raw_response(self):
        page = await self.client.get(f"{self.model_base}/history", params={"kind": "attempts"})
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 1)
        self.assertNotIn("raw_response", page.json()["items"][0])
        self.assertNotIn("exception", page.json()["items"][0])

    async def test_attempt_raw_is_exact_and_cannot_be_read_through_another_model(self):
        attempt = replace(self.workspace.attempts[0], raw_response="original failed/output evidence")
        self.workspace.attempts[0] = attempt
        response = await self.client.get(f"{self.model_base}/attempts/{attempt.attempt_id}/raw", params={"offset": 9, "limit": 6})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["text"], "failed")
        other = await self.client.get(f"{self.base}/models/{MOCK_MODEL_IDS[1]}/attempts/{attempt.attempt_id}/raw")
        self.assertEqual(other.status_code, 404)

    async def test_model_byte_budget_rejects_before_paid_solve_and_preserves_confirmed_state(self):
        self.service.confirm(self.session.session_id, self.workspace.model_id)
        self.workspace._history_bytes = self.service.MAX_HISTORY_BYTES - 1024
        original = self.service.providers.get_provider
        provider = Mock(wraps=original(self.workspace.model_id))
        self.service.providers.get_provider = lambda model_id: provider
        response = await self.client.post(f"{self.model_base}/solve")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(provider.solve.call_count, 0)
        self.assertEqual(self.workspace.status.value, "CONFIRMED")
        self.assertEqual(len(self.workspace.attempts), 1)
        self.assertEqual((await self.client.get(self.base)).status_code, 200)

    async def test_three_large_current_results_fit_response_budget_without_losing_evidence(self):
        self.service.extract(self.session.session_id)
        for workspace in self.session.models.values():
            data = workspace.draft.data
            large_data = replace(data, rooms=(replace(data.rooms[0], name="x" * 63000),) + data.rooms[1:])
            workspace.proposal = replace(workspace.proposal, data=large_data, raw_response="\x00" * 1048576)
            workspace.draft = replace(workspace.draft, data=large_data)
            workspace.problem_management.repository.save_draft(workspace.draft)
            self.service.confirm(self.session.session_id, workspace.model_id)
            workspace.solution = TimetableSolution(
                "solution", workspace.snapshot.snapshot_id, workspace.model_id,
                tuple(LessonAssignment("r" * 128, index, "s" * 128, "o" * 128) for index in range(500)),
            )
            workspace.verification = TimetableVerificationResult(
                "solution", workspace.snapshot.snapshot_id, VerificationStatus.FAILED,
                tuple(ConstraintViolation("conflict", "m" * 200, requirement_ids=("r" * 128,), slot_ids=("s" * 128,)) for _ in range(800)),
            )
            self.assertLess(len(json.dumps(asdict(workspace.solution), separators=(",", ":")).encode()), 256 * 1024)
            self.assertLess(len(json.dumps(asdict(workspace.verification), separators=(",", ":")).encode()), 512 * 1024)
            workspace.snapshots = [workspace.snapshot] * 100
            workspace.solutions = [workspace.solution] * 100
            workspace.verifications = [workspace.verification] * 100
            workspace.attempts = [replace(workspace.attempts[0], raw_response="\x00" * 1048576)] * 100
        response = await self.client.get(self.base)
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 4 * 1024 * 1024)
        public = response.json()["models"][self.workspace.model_id]
        self.assertEqual(len(public["solution"]["assignments"]), 500)
        self.assertEqual(len(public["verification"]["violations"]), 800)
        self.assertEqual(len(public["attempts"]), 5)
        self.assertEqual(public["history_counts"]["attempts"], 100)

    async def test_large_unconfirmed_edit_history_rejects_before_changing_current_draft(self):
        rejection = None
        for index in range(1000):
            before = self.workspace.draft
            data = before.data
            desired = replace(data, rooms=(replace(data.rooms[0], name=str(index) + "x" * 58000),) + data.rooms[1:])
            try:
                self.service.edit_draft(self.session.session_id, self.workspace.model_id, desired)
            except ValueError as error:
                rejection = error
                self.assertIs(self.workspace.draft, before)
                break
        self.assertIsNotNone(rejection)
        self.assertLess(index, 999)
        self.assertEqual((await self.client.get(self.base)).status_code, 200)


class SessionRetentionBudgetTests(unittest.TestCase):
    def make_service(self, repository):
        return WorkflowService(repository, MockProviderFactory(), TimetableVerifier())

    def test_session_count_limit_keeps_existing_session_readable(self):
        repository = InMemorySessionRepository(max_sessions=1)
        service = self.make_service(repository)
        first = service.create_session("first")
        with self.assertRaisesRegex(ValueError, "[Ll]imit|[Cc]apacity"):
            service.create_session("second")
        self.assertIs(repository.get(first.session_id), first)

    def test_byte_budget_is_shared_and_released_when_session_expires(self):
        repository = InMemorySessionRepository(max_retained_bytes=5 * 1024 * 1024)
        service = self.make_service(repository)
        first = service.create_session(MOCK_DEMO_PROMPT)
        second = service.create_session(MOCK_DEMO_PROMPT)
        repository.reserve_history(first.session_id, 2 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "[Hh]istory|[Bb]udget|[Cc]apacity"):
            service.extract(second.session_id, [MOCK_MODEL_IDS[0]])
        self.assertEqual(second.models[MOCK_MODEL_IDS[0]].status.value, "PENDING")
        self.assertEqual(second.models[MOCK_MODEL_IDS[0]].attempts, [])
        first.last_accessed_at -= timedelta(hours=3)
        repository.get(second.session_id)
        service.extract(second.session_id, [MOCK_MODEL_IDS[0]])
        self.assertEqual(second.models[MOCK_MODEL_IDS[0]].status.value, "DRAFT")

    def test_small_paid_outputs_release_unused_reservation_for_next_session(self):
        repository = InMemorySessionRepository(max_retained_bytes=5 * 1024 * 1024)
        service = self.make_service(repository)
        for _ in range(3):
            session = service.create_session(MOCK_DEMO_PROMPT)
            service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
            self.assertEqual(session.models[MOCK_MODEL_IDS[0]].status.value, "DRAFT")

    def test_shared_capacity_failure_precedes_solve_status_and_vendor_call(self):
        repository = InMemorySessionRepository(max_retained_bytes=5 * 1024 * 1024)
        service = self.make_service(repository)
        session = service.create_session(MOCK_DEMO_PROMPT)
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        workspace = service.confirm(session.session_id, MOCK_MODEL_IDS[0])
        repository.reserve_history(session.session_id, 2 * 1024 * 1024)
        provider = Mock(wraps=service.providers.get_provider(workspace.model_id))
        service.providers.get_provider = lambda model_id: provider
        with self.assertRaisesRegex(ValueError, "[Cc]apacity|[Bb]udget"):
            service.solve(session.session_id, workspace.model_id)
        self.assertEqual(workspace.status.value, "CONFIRMED")
        self.assertEqual(provider.solve.call_count, 0)

    def test_whole_extraction_selection_reservation_rolls_back_on_global_budget_failure(self):
        repository = InMemorySessionRepository(max_retained_bytes=5 * 1024 * 1024)
        service = self.make_service(repository)
        session = service.create_session(MOCK_DEMO_PROMPT)
        with self.assertRaises(ValueError):
            service.extract(session.session_id)
        self.assertTrue(all(workspace.status.value == "PENDING" for workspace in session.models.values()))
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        self.assertEqual(session.models[MOCK_MODEL_IDS[0]].status.value, "DRAFT")

    def test_new_target_is_admitted_before_pending_replay_and_original_retry_still_works(self):
        repository = InMemorySessionRepository(max_retained_bytes=5 * 1024 * 1024)
        service = self.make_service(repository)
        session = service.create_session(MOCK_DEMO_PROMPT)
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        workspace = session.models[MOCK_MODEL_IDS[0]]
        data = workspace.draft.data
        pending_target = replace(data, rooms=(replace(data.rooms[0], name="first correction"),) + data.rooms[1:])
        original_apply = workspace.problem_management.apply_edit
        workspace.problem_management.apply_edit = Mock(side_effect=RuntimeError("temporary local write failure"))
        with self.assertRaises(RuntimeError):
            service.edit_draft(session.session_id, workspace.model_id, pending_target)
        workspace.problem_management.apply_edit = original_apply
        before, pending = workspace.draft, workspace._pending_edit
        repository.reserve_history(session.session_id, 5 * 1024 * 1024 - 100 * 1024)
        new_target = replace(data, rooms=(replace(data.rooms[0], name="x" * 58000),) + data.rooms[1:])
        with self.assertRaises(ValueError):
            service.edit_draft(session.session_id, workspace.model_id, new_target)
        self.assertIs(workspace.draft, before)
        self.assertIs(workspace._pending_edit, pending)
        self.assertEqual(workspace.edit_count, 0)
        service.edit_draft(session.session_id, workspace.model_id, pending_target)
        self.assertEqual(workspace.draft.data, pending_target)
        self.assertIsNone(workspace._pending_edit)
        self.assertEqual(workspace.edit_count, 1)

    def test_invalid_confirmations_do_not_consume_budget_or_prevent_later_repair(self):
        repository = InMemorySessionRepository()
        service = self.make_service(repository)
        session = service.create_session("unrecognized mock prompt")
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        workspace = session.models[MOCK_MODEL_IDS[0]]
        initial_bytes = workspace._history_bytes
        initial_total = repository._total_retained_bytes
        for _ in range(100):
            with self.assertRaises(ValueError):
                service.confirm(session.session_id, workspace.model_id)
        self.assertEqual(workspace._history_bytes, initial_bytes)
        self.assertEqual(repository._total_retained_bytes, initial_total)
        self.assertEqual(workspace._history_snapshot_admissions, 0)
        valid_data = MockProviderFactory().get_provider(workspace.model_id).extract(
            replace(session.problem, prompt=MOCK_DEMO_PROMPT)
        ).data
        service.edit_draft(session.session_id, workspace.model_id, valid_data)
        service.confirm(session.session_id, workspace.model_id)
        charged_once = workspace._history_bytes
        service.confirm(session.session_id, workspace.model_id)
        self.assertEqual(workspace._history_bytes, charged_once)
        self.assertEqual(workspace.snapshot.version, 1)
        self.assertEqual(len(workspace.snapshots), 1)
        self.assertEqual(workspace._history_snapshot_admissions, 1)

    def test_private_raw_controls_are_charged_as_payload_bytes_and_paid_proposal_is_cached(self):
        raw = "\x00" * 1048576

        class RawExecution:
            def execute(self, operation, calls):
                return {model_id: AICallOutcome(result=replace(call(), raw_response=raw), raw_response=raw)
                        for model_id, call in calls.items()}

        repository = InMemorySessionRepository(max_retained_bytes=5 * 1024 * 1024)
        service = WorkflowService(repository, MockProviderFactory(), TimetableVerifier(), execution=RawExecution())
        session = service.create_session(MOCK_DEMO_PROMPT)
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        workspace = session.models[MOCK_MODEL_IDS[0]]
        self.assertEqual(workspace.status.value, "DRAFT")
        self.assertEqual(workspace.proposal.raw_response, raw)
        self.assertEqual(workspace.attempts[0].raw_response, raw)
        self.assertLess(workspace._history_bytes, 3 * 1024 * 1024)
        self.assertEqual(workspace._history_reserved_bytes, 0)
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        self.assertEqual(len(workspace.attempts), 1)

    def test_confirmation_replays_known_snapshot_after_persisted_write_then_exception(self):
        for failure_layer in ("confirm", "repository"):
            with self.subTest(failure_layer=failure_layer):
                repository = InMemorySessionRepository()
                service = self.make_service(repository)
                session = service.create_session(MOCK_DEMO_PROMPT)
                service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
                workspace = session.models[MOCK_MODEL_IDS[0]]
                pm = workspace.problem_management
                target = pm if failure_layer == "confirm" else pm.repository
                method_name = "confirm" if failure_layer == "confirm" else "save_snapshot"
                original = getattr(target, method_name)

                def persisted_then_raised(*args):
                    original(*args)
                    raise RuntimeError("local acknowledgement lost")

                setattr(target, method_name, persisted_then_raised)
                with self.assertRaises(RuntimeError):
                    service.confirm(session.session_id, workspace.model_id)
                self.assertEqual(len(pm.repository.snapshots), 1)
                stored = next(iter(pm.repository.snapshots.values()))
                charged = workspace._history_bytes
                total_charged = repository._total_retained_bytes
                setattr(target, method_name, original)
                service.confirm(session.session_id, workspace.model_id)
                self.assertIs(workspace.snapshot, stored)
                self.assertEqual(workspace.snapshots, [stored])
                self.assertEqual(len(pm.repository.snapshots), 1)
                self.assertEqual(workspace.snapshot.version, 1)
                self.assertEqual(workspace._history_snapshot_admissions, 1)
                self.assertEqual(workspace._history_bytes, charged)
                self.assertEqual(repository._total_retained_bytes, total_charged)
