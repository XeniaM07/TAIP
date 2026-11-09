
"""Timetable problem management service."""

import json
from uuid import uuid4
from dataclasses import asdict
from taip.problem_management.timetable_audit import (
    InMemoryAuditLog,
    audit_operation,
)
from taip.common.timetable_contracts import (
    ExtractionProposal,
    ProblemDraft,
    TimetableProblemSnapshot,
    SchoolClass,
    Teacher,
    Subject,
    Room,
    TimeSlot,
    Weekday,
    TeachingRequirement,
    TimetableConstraint,
    ConstraintType,
    ConstraintSeverity,
    TeacherUnavailable,
    RoomUnavailable,
    ClassUnavailable,
    RoomFeatureRequired,
    MaxClassPeriodsPerDay,
    TeacherPreferredSlots,
    MinimizeTeacherGaps,
    MinimizeClassGaps,
    BalanceClassDailyLoad,
)
from taip.common.timetable_protocols import (
    DraftValidationResult,
    EditOperation,
)
from taip.problem_management.repository import InMemoryProblemRepository
from taip.problem_management.timetable_commands import (
    AddEntityCommand,
    UpdateEntityCommand,
    RemoveEntityCommand,
)
from taip.problem_management.timetable_validation import TimetableDraftValidator


class TimetableProblemService:
    """Manage editable timetable drafts and confirmed snapshots."""

    def __init__(self, repository: InMemoryProblemRepository) -> None:
        self.repository = repository
        self.audit_log = InMemoryAuditLog()
        self._confirmation_requests: dict[str, tuple[TimetableProblemSnapshot, int]] = {}
        self._confirmed_tokens: dict[str, TimetableProblemSnapshot] = {}
        self._active_confirmation: tuple[TimetableProblemSnapshot, int] | None = None

    def create_draft(self, proposal: ExtractionProposal) -> ProblemDraft:
        """Create an editable draft from an AI extraction proposal."""
        draft = ProblemDraft(
            draft_id=str(uuid4()),
            proposal_id=proposal.proposal_id,
            problem_id=proposal.problem_id,
            model_id=proposal.model_id,
            revision=1,
            data=proposal.data,
        )

        self.repository.save_draft(draft)
        return draft

    def get_draft(self, draft_id: str) -> ProblemDraft:
        """Retrieve a draft from temporary storage."""
        return self.repository.get_draft(draft_id)

    def apply_command(self, draft_id: str, command) -> ProblemDraft:
        """Execute a command and record the previous state."""
        draft = self.repository.get_draft(draft_id)

        updated_draft = command.execute(draft)

        history = self.repository.get_history(draft_id)
        history.record(draft)

        self.repository.save_draft(updated_draft)
        return updated_draft
    @audit_operation("undo")
    def undo(self, draft_id: str) -> ProblemDraft:
        """Restore the previous draft state."""
        current = self.repository.get_draft(draft_id)
        history = self.repository.get_history(draft_id)

        previous = history.undo(current)
        self.repository.save_draft(previous)
        return previous
    @audit_operation("redo")
    def redo(self, draft_id: str) -> ProblemDraft:
        """Restore an undone draft state."""
        current = self.repository.get_draft(draft_id)
        history = self.repository.get_history(draft_id)

        restored = history.redo(current)
        self.repository.save_draft(restored)
        return restored

    def validate(self, draft_id: str) -> DraftValidationResult:
        """Validate a timetable draft before confirmation."""
        draft = self.repository.get_draft(draft_id)

        validator = TimetableDraftValidator()
        return validator.validate(draft)
    @audit_operation("confirm")
    def confirm(
        self,
        draft_id: str,
        original_prompt: str,
    ) -> TimetableProblemSnapshot:
        """Confirm a valid draft and create a versioned snapshot."""

        if not original_prompt.strip():
            raise ValueError("Original prompt cannot be empty.")

        draft = self.repository.get_draft(draft_id)

        validation = self.validate(draft_id)
        if not validation.valid:
            raise ValueError(
                f"Cannot confirm invalid draft: {validation.issues}"
            )

        snapshot = self._new_snapshot(draft, original_prompt) if self._active_confirmation is None else self._active_confirmation[0]
        if self._active_confirmation is not None:
            self._check_confirmation_request(draft, original_prompt, self._active_confirmation)

        self.repository.save_snapshot(snapshot)
        return snapshot

    def _new_snapshot(self, draft: ProblemDraft, original_prompt: str) -> TimetableProblemSnapshot:
        versions = [
            snapshot.version
            for snapshot in self.repository.snapshots.values()
            if snapshot.draft_id == draft.draft_id
        ]

        return TimetableProblemSnapshot(
            snapshot_id=str(uuid4()),
            draft_id=draft.draft_id,
            problem_id=draft.problem_id,
            model_id=draft.model_id,
            version=max(versions, default=0) + 1,
            original_prompt=original_prompt,
            data=draft.data,
        )

    @staticmethod
    def _check_confirmation_request(
        draft: ProblemDraft, original_prompt: str,
        request: tuple[TimetableProblemSnapshot, int],
    ) -> None:
        expected, revision = request
        if (draft.revision != revision or draft.draft_id != expected.draft_id
                or draft.problem_id != expected.problem_id or draft.model_id != expected.model_id
                or draft.data != expected.data or original_prompt != expected.original_prompt):
            raise ValueError("The confirmation token belongs to a different draft revision or prompt.")

    def ensure_confirmed(
        self, draft_id: str, original_prompt: str, confirmation_id: str,
    ) -> TimetableProblemSnapshot:
        """Replay one local confirmation with an ID allocated before any write."""
        draft = self.get_draft(draft_id)
        request = self._confirmation_requests.get(confirmation_id)
        if request is None:
            if not original_prompt.strip() or not self.validate(draft_id).valid:
                raise ValueError("Cannot confirm an invalid draft or empty original prompt.")
            if len(self._confirmation_requests) >= 100:
                raise ValueError("This draft service has reached its confirmation history limit.")
            request = (self._new_snapshot(draft, original_prompt), draft.revision)
            self._confirmation_requests[confirmation_id] = request
        self._check_confirmation_request(draft, original_prompt, request)
        expected = request[0]
        if confirmation_id in self._confirmed_tokens:
            return self._confirmed_tokens[confirmation_id]
        try:
            stored = self.get_snapshot(expected.snapshot_id)
        except KeyError:
            pass
        else:
            if stored != expected:
                raise ValueError("The reserved snapshot identity contains different confirmation data.")
            self._confirmed_tokens[confirmation_id] = stored
            return stored

        previous = self._active_confirmation
        self._active_confirmation = request
        try:
            snapshot = self.confirm(draft_id, original_prompt)
            if snapshot != expected:
                raise ValueError("Confirmation did not return its reserved snapshot.")
            self._confirmed_tokens[confirmation_id] = snapshot
            return snapshot
        except Exception:
            # The repository or an enclosing port wrapper can commit and then
            # lose its acknowledgement. Cache only an exact known-ID match.
            try:
                stored = self.get_snapshot(expected.snapshot_id)
            except KeyError:
                pass
            else:
                if stored == expected:
                    self._confirmed_tokens[confirmation_id] = stored
            raise
        finally:
            self._active_confirmation = previous

    def get_snapshot(
        self,
        snapshot_id: str,
    ) -> TimetableProblemSnapshot:
        """Retrieve a confirmed snapshot from memory."""
        return self.repository.get_snapshot(snapshot_id)
    @audit_operation("edit")
    def apply_edit(
        self,
        draft_id: str,
        operation: EditOperation,
    ) -> ProblemDraft:
        """Apply an edit operation using the Command pattern."""
        entity_classes = {
            "class": SchoolClass,
            "teacher": Teacher,
            "subject": Subject,
            "room": Room,
            "time_slot": TimeSlot,
            "requirement": TeachingRequirement,
            "constraint": TimetableConstraint,
        }

        entity_id_fields = {
            "class": "class_id",
            "teacher": "teacher_id",
            "subject": "subject_id",
            "room": "room_id",
            "time_slot": "slot_id",
            "requirement": "requirement_id",
            "constraint": "constraint_id",
        }

        if operation.entity_type not in entity_classes:
            raise ValueError(
                f"Unsupported entity type: {operation.entity_type}"
            )

        entity_class = entity_classes[operation.entity_type]
        id_field = entity_id_fields[operation.entity_type]

        def deserialize_entity(raw_json: str):
            """Convert JSON into a typed timetable entity."""
            data = json.loads(raw_json)

            if not isinstance(data, dict):
                raise ValueError("Entity JSON must be an object.")

            if operation.entity_type == "constraint":
                constraint_type = ConstraintType(data["type"])

                parameter_classes = {
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

                parameter_class = parameter_classes[constraint_type]
                parameters = dict(data["parameters"])

                if "slot_ids" in parameters:
                    parameters["slot_ids"] = tuple(parameters["slot_ids"])

                data["type"] = constraint_type
                data["severity"] = ConstraintSeverity(data["severity"])
                data["parameters"] = parameter_class(**parameters)

            elif operation.entity_type == "time_slot":
                data["day"] = Weekday(data["day"])

            elif operation.entity_type == "teacher":
                if "subject_ids" in data:
                    data["subject_ids"] = tuple(data["subject_ids"])

            elif operation.entity_type == "room":
                if "features" in data:
                    data["features"] = tuple(data["features"])

            return entity_class(**data)
        if operation.action in ("update", "remove"):
            if operation.before_json is not None:
                current_draft = self.get_draft(draft_id)

                collection_names = {
                    "class": "classes",
                    "teacher": "teachers",
                    "subject": "subjects",
                    "room": "rooms",
                    "time_slot": "time_slots",
                    "requirement": "requirements",
                    "constraint": "constraints",
                }

                collection = getattr(
                    current_draft.data,
                    collection_names[operation.entity_type],
                )

                current_entity = next(
                    (
                        item for item in collection
                        if getattr(item, id_field) == operation.entity_id
                    ),
                    None,
                )

                if current_entity is None:
                    raise ValueError("Entity does not exist.")

                expected_entity = deserialize_entity(
                    operation.before_json
                )

                if current_entity != expected_entity:
                    raise ValueError(
                        "Entity was modified since before_json was created."
                    )
        if operation.action in ("add", "update"):
            if operation.after_json is None:
                raise ValueError(
                    f"{operation.action} requires after_json."
                )

            entity = deserialize_entity(operation.after_json)

            actual_id = getattr(entity, id_field)

            if actual_id != operation.entity_id:
                raise ValueError(
                    "Entity ID does not match operation.entity_id."
                )

            if operation.action == "add":
                command = AddEntityCommand(
                    operation.entity_type,
                    entity,
                )
            else:
                command = UpdateEntityCommand(
                    operation.entity_type,
                    entity,
                )

            return self.apply_command(draft_id, command)

        if operation.action == "remove":
            command = RemoveEntityCommand(
                operation.entity_type,
                operation.entity_id,
            )
            return self.apply_command(draft_id, command)

        raise ValueError(
            f"Unsupported action: {operation.action}"
        )
