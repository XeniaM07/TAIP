import unittest

from taip.common.timetable_contracts import (
    ExtractionProposal,
    TimetableData,
)
import json

from taip.common.timetable_protocols import EditOperation
from taip.problem_management.repository import InMemoryProblemRepository
from taip.problem_management.service import TimetableProblemService

from taip.common.timetable_contracts import (
    SchoolClass,
    Teacher,
    Subject,
    Room,
    TimeSlot,
    Weekday,
    TeachingRequirement,
)
from taip.common.timetable_protocols import ProblemManagementPort
class TestTimetableProblemService(unittest.TestCase):

    def setUp(self):
        self.service = TimetableProblemService(
            InMemoryProblemRepository()
        )

    def test_separate_drafts_per_model(self):
        models = ("gemini", "llama", "qwen")

        drafts = [
            self.service.create_draft(
                ExtractionProposal(
                    proposal_id=f"proposal_{model}",
                    problem_id="problem1",
                    model_id=model,
                    data=TimetableData(),
                )
            )
            for model in models
        ]

        self.assertEqual(len({d.draft_id for d in drafts}), 3)
        self.assertEqual(
            {d.model_id for d in drafts},
            set(models),
        )

        for draft in drafts:
            self.assertEqual(
                self.service.get_draft(draft.draft_id),
                draft,
            )
    def test_edit_undo_redo(self):
        draft = self.service.create_draft(
            ExtractionProposal(
                "p1", "problem1", "gemini", TimetableData()
            )
        )

        def edit(action, op_id, name=None):
            return EditOperation(
                operation_id=op_id,
                entity_type="teacher",
                entity_id="t1",
                action=action,
                before_json=None,
                after_json=(
                    json.dumps({"teacher_id": "t1", "name": name})
                    if name is not None
                    else None
                ),
            )

        # ADD
        updated = self.service.apply_edit(
            draft.draft_id, edit("add", "op1", "Popescu")
        )
        self.assertEqual(updated.data.teachers[0].name, "Popescu")

        # UPDATE
        updated = self.service.apply_edit(
            draft.draft_id, edit("update", "op2", "Ionescu")
        )
        self.assertEqual(updated.data.teachers[0].name, "Ionescu")

        # UNDO
        updated = self.service.undo(draft.draft_id)
        self.assertEqual(updated.data.teachers[0].name, "Popescu")

        # REDO
        updated = self.service.redo(draft.draft_id)
        self.assertEqual(updated.data.teachers[0].name, "Ionescu")

        # REMOVE
        updated = self.service.apply_edit(
            draft.draft_id, edit("remove", "op3")
        )
        self.assertEqual(len(updated.data.teachers), 0)
    def test_invalid_draft_cannot_be_confirmed(self):
        draft = self.service.create_draft(
            ExtractionProposal(
                "p1",
                "problem1",
                "gemini",
                TimetableData(),
            )
        )

        # Verificam validarea
        result = self.service.validate(draft.draft_id)

        self.assertFalse(result.valid)
        self.assertEqual(len(result.issues), 6)

        self.assertTrue(
            all(issue.code == "MISSING_DATA" for issue in result.issues)
        )

        # Confirmarea trebuie sa fie refuzata
        with self.assertRaises(ValueError):
            self.service.confirm(
                draft.draft_id,
                "Genereaza un orar scolar",
            )

        # Nu trebuie sa existe snapshoturi salvate
        self.assertEqual(len(self.service.repository.snapshots), 0)
    def test_versioned_immutable_snapshots(self):
        data = TimetableData(
            classes=(SchoolClass("c1", "Clasa 5A"),),
            teachers=(Teacher("t1", "Popescu"),),
            subjects=(Subject("s1", "Matematica"),),
            rooms=(Room("r1", "Sala 101"),),
            time_slots=(TimeSlot("slot1", Weekday.MONDAY, 1),),
            requirements=(
                TeachingRequirement("req1", "c1", "s1", "t1", 1),
            ),
        )

        draft = self.service.create_draft(
            ExtractionProposal("p1", "problem1", "gemini", data)
        )

        self.assertTrue(self.service.validate(draft.draft_id).valid)

        # Confirmam prima versiune
        snapshot1 = self.service.confirm(
            draft.draft_id, "Genereaza un orar"
        )

        self.assertEqual(snapshot1.version, 1)

        # Modificam profesorul din draft
        operation = EditOperation(
            operation_id="op1",
            entity_type="teacher",
            entity_id="t1",
            action="update",
            before_json=None,
            after_json=json.dumps({
                "teacher_id": "t1",
                "name": "Ionescu",
            }),
        )

        self.service.apply_edit(draft.draft_id, operation)

        # Confirmam versiunea a doua
        snapshot2 = self.service.confirm(
            draft.draft_id, "Genereaza un orar"
        )

        self.assertEqual(snapshot2.version, 2)
        self.assertNotEqual(snapshot1.snapshot_id, snapshot2.snapshot_id)

        # Prima versiune trebuie sa ramana neschimbata
        self.assertEqual(snapshot1.data.teachers[0].name, "Popescu")
        self.assertEqual(snapshot2.data.teachers[0].name, "Ionescu")

        # Ambele snapshoturi trebuie sa poata fi recuperate
        self.assertEqual(
            self.service.get_snapshot(snapshot1.snapshot_id),
            snapshot1,
        )
        self.assertEqual(
            self.service.get_snapshot(snapshot2.snapshot_id),
            snapshot2,
        )
    def test_aop_audit_success_and_failure(self):
        draft = self.service.create_draft(
            ExtractionProposal(
                "p1", "problem1", "gemini", TimetableData()
            )
        )

        operation = EditOperation(
            operation_id="op1",
            entity_type="teacher",
            entity_id="t1",
            action="add",
            before_json=None,
            after_json=json.dumps({
                "teacher_id": "t1",
                "name": "Popescu",
            }),
        )

        # Prima adaugare reuseste
        self.service.apply_edit(draft.draft_id, operation)

        # A doua adaugare trebuie sa esueze (ID duplicat)
        with self.assertRaises(ValueError):
            self.service.apply_edit(draft.draft_id, operation)

        events = self.service.audit_log.events

        self.assertEqual(len(events), 2)

        self.assertEqual(events[0].operation, "add")
        self.assertTrue(events[0].success)

        self.assertEqual(events[1].operation, "add")
        self.assertFalse(events[1].success)

        for event in events:
            self.assertEqual(event.draft_id, draft.draft_id)
            self.assertGreaterEqual(event.duration_ms, 0)
    def test_implements_problem_management_port(self):
        self.assertIsInstance(
            self.service,
            ProblemManagementPort,
        )

    def test_undo_history_isolated_between_drafts(self):
        gemini = self.service.create_draft(
            ExtractionProposal(
                "p1", "problem1", "gemini", TimetableData()
            )
        )

        qwen = self.service.create_draft(
            ExtractionProposal(
                "p2", "problem1", "qwen", TimetableData()
            )
        )

        operation = EditOperation(
            operation_id="op1",
            entity_type="teacher",
            entity_id="t1",
            action="add",
            before_json=None,
            after_json=json.dumps({
                "teacher_id": "t1",
                "name": "Popescu",
            }),
        )

        self.service.apply_edit(gemini.draft_id, operation)

        # Gemini are un profesor, Qwen ramane nemodificat.
        self.assertEqual(
            len(self.service.get_draft(gemini.draft_id).data.teachers), 1
        )
        self.assertEqual(
            len(self.service.get_draft(qwen.draft_id).data.teachers), 0
        )

        # Qwen nu trebuie sa poata anula modificarile Gemini.
        with self.assertRaises(ValueError):
            self.service.undo(qwen.draft_id)

        # Gemini isi poate anula propria modificare.
        restored = self.service.undo(gemini.draft_id)
        self.assertEqual(len(restored.data.teachers), 0)

        # Gemini poate reface modificarea.
        restored = self.service.redo(gemini.draft_id)
        self.assertEqual(len(restored.data.teachers), 1)

        # Qwen ramane nemodificat.
        self.assertEqual(
            len(self.service.get_draft(qwen.draft_id).data.teachers), 0
        )
    def test_invalid_edit_operations(self):
        draft = self.service.create_draft(
            ExtractionProposal(
                "p1", "problem1", "gemini", TimetableData()
            )
        )

        def operation(action, entity_id, name):
            return EditOperation(
                operation_id="op1",
                entity_type="teacher",
                entity_id=entity_id,
                action=action,
                before_json=None,
                after_json=json.dumps({
                    "teacher_id": "t1",
                    "name": name,
                }),
            )

        # Prima adaugare este valida
        self.service.apply_edit(
            draft.draft_id,
            operation("add", "t1", "Popescu"),
        )

        # ID duplicat
        with self.assertRaises(ValueError):
            self.service.apply_edit(
                draft.draft_id,
                operation("add", "t1", "Ionescu"),
            )

        # Profesor inexistent
        with self.assertRaises(ValueError):
            self.service.apply_edit(
                draft.draft_id,
                operation("update", "t2", "Georgescu"),
            )

        # ID-ul operatiei nu corespunde cu ID-ul din JSON
        with self.assertRaises(ValueError):
            self.service.apply_edit(
                draft.draft_id,
                operation("add", "t3", "Marinescu"),
            )

        # Operatiile esuate nu trebuie sa modifice draftul
        current = self.service.get_draft(draft.draft_id)

        self.assertEqual(len(current.data.teachers), 1)
        self.assertEqual(current.data.teachers[0].name, "Popescu")
        self.assertEqual(current.revision, 2)
    def test_update_nonexistent_and_redo_cleared(self):
        draft = self.service.create_draft(
            ExtractionProposal(
                "p1", "problem1", "gemini", TimetableData()
            )
        )

        def edit(action, entity_id, name):
            return EditOperation(
                operation_id="op1",
                entity_type="teacher",
                entity_id=entity_id,
                action=action,
                before_json=None,
                after_json=json.dumps({
                    "teacher_id": entity_id,
                    "name": name,
                }),
            )

        # Nu putem actualiza un profesor inexistent.
        with self.assertRaises(ValueError):
            self.service.apply_edit(
                draft.draft_id,
                edit("update", "t99", "Inexistent"),
            )

        # Adaugam un profesor.
        self.service.apply_edit(
            draft.draft_id,
            edit("add", "t1", "Popescu"),
        )

        # Anulam adaugarea.
        self.service.undo(draft.draft_id)

        # Facem o modificare noua dupa Undo.
        self.service.apply_edit(
            draft.draft_id,
            edit("add", "t2", "Ionescu"),
        )

        # Redo trebuie sa fie gol.
        with self.assertRaises(ValueError):
            self.service.redo(draft.draft_id)

        current = self.service.get_draft(draft.draft_id)

        self.assertEqual(len(current.data.teachers), 1)
        self.assertEqual(current.data.teachers[0].name, "Ionescu")
if __name__ == "__main__":
    unittest.main()