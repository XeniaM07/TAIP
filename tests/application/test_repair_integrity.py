"""Draft replacement must preserve identity and repair ambiguous AI entities."""

from dataclasses import replace
from unittest import TestCase

from taip.application.workflow import (
    MOCK_DEMO_PROMPT, MOCK_MODEL_IDS, InMemorySessionRepository,
    MockProviderFactory, WorkflowService,
)
from taip.benchmark.timetable_service import TimetableBenchmarkService
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


class DraftRepairIntegrityTests(TestCase):
    def make_service(self):
        service = WorkflowService(
            InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier(),
            TimetableBenchmarkService,
        )
        session = service.create_session(MOCK_DEMO_PROMPT)
        service.extract(session.session_id, [MOCK_MODEL_IDS[0]])
        return service, session, session.models[MOCK_MODEL_IDS[0]]

    def test_duplicate_incoming_ids_reject_without_mutating_confirmed_state(self):
        service, session, workspace = self.make_service()
        service.confirm(session.session_id, workspace.model_id)
        draft, snapshot = workspace.draft, workspace.snapshot
        events = len(workspace.problem_management.audit_log.events)
        for collection in ("classes", "teachers", "subjects", "rooms", "time_slots", "requirements"):
            with self.subTest(collection=collection):
                entities = getattr(draft.data, collection)
                invalid = replace(draft.data, **{collection: entities + (entities[0],)})
                with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
                    service.edit_draft(session.session_id, workspace.model_id, invalid)
                self.assertIs(workspace.draft, draft)
                self.assertIs(workspace.snapshot, snapshot)
                self.assertEqual(workspace.edit_count, 0)
                self.assertEqual(len(workspace.problem_management.audit_log.events), events)

    def test_duplicate_ai_rows_are_removed_then_user_selected_row_is_added(self):
        for select_last in (False, True):
            with self.subTest(select_last=select_last):
                service, session, workspace = self.make_service()
                data = workspace.draft.data
                changed_room = replace(data.rooms[0], name="Conflicting AI room")
                original_data = replace(data, rooms=data.rooms + (changed_room,))
                proposal = replace(workspace.proposal, data=original_data)
                workspace.proposal = proposal
                workspace.draft = workspace.problem_management.create_draft(proposal)
                # Keep the original proposal in the real benchmark as well.
                record = session.benchmark.repository.get_run(
                    session.session_id, session.comparison_id, workspace.model_id,
                )
                session.benchmark.repository.save_run(replace(record, proposal=proposal))
                selected = changed_room if select_last else data.rooms[0]
                desired = replace(data, rooms=(selected, data.rooms[1]))

                service.edit_draft(session.session_id, workspace.model_id, desired)

                self.assertEqual(len(workspace.draft.data.rooms), 2)
                self.assertEqual({row.room_id: row for row in workspace.draft.data.rooms}, {row.room_id: row for row in desired.rooms})
                self.assertEqual(workspace.proposal.data.rooms, original_data.rooms)
                recorded = session.benchmark.repository.get_run(
                    session.session_id, session.comparison_id, workspace.model_id,
                )
                self.assertEqual([operation.action for operation in recorded.edits], ["remove", "add"])
                self.assertEqual(recorded.proposal.data, original_data)
                service.confirm(session.session_id, workspace.model_id)
                self.assertEqual(workspace.snapshot.data, workspace.draft.data)
