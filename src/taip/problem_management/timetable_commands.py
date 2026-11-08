"""Command pattern for timetable entity editing."""

from dataclasses import replace
from typing import Any

from taip.common.timetable_contracts import ProblemDraft


ENTITY_COLLECTIONS = {
    "class": "classes",
    "teacher": "teachers",
    "subject": "subjects",
    "room": "rooms",
    "time_slot": "time_slots",
    "requirement": "requirements",
    "constraint": "constraints",
}

ENTITY_ID_FIELDS = {
    "class": "class_id",
    "teacher": "teacher_id",
    "subject": "subject_id",
    "room": "room_id",
    "time_slot": "slot_id",
    "requirement": "requirement_id",
    "constraint": "constraint_id",
}


class AddEntityCommand:
    """Add an entity to a timetable draft."""

    def __init__(self, entity_type: str, entity: Any) -> None:
        self.entity_type = entity_type
        self.entity = entity

    def execute(self, draft: ProblemDraft) -> ProblemDraft:
        if self.entity_type not in ENTITY_COLLECTIONS:
            raise ValueError(f"Unknown entity type: {self.entity_type}")

        collection_name = ENTITY_COLLECTIONS[self.entity_type]
        id_field = ENTITY_ID_FIELDS[self.entity_type]

        current_entities = getattr(draft.data, collection_name)
        entity_id = getattr(self.entity, id_field)

        if any(
            getattr(existing, id_field) == entity_id
            for existing in current_entities
        ):
            raise ValueError(
                f"{self.entity_type} '{entity_id}' already exists."
            )

        updated_entities = current_entities + (self.entity,)

        updated_data = replace(
            draft.data,
            **{collection_name: updated_entities},
        )

        return replace(
            draft,
            data=updated_data,
            revision=draft.revision + 1,
        )
class UpdateEntityCommand:
    """Replace an existing timetable entity."""

    def __init__(self, entity_type: str, entity: Any) -> None:
        self.entity_type = entity_type
        self.entity = entity

    def execute(self, draft: ProblemDraft) -> ProblemDraft:
        if self.entity_type not in ENTITY_COLLECTIONS:
            raise ValueError(f"Unknown entity type: {self.entity_type}")

        collection_name = ENTITY_COLLECTIONS[self.entity_type]
        id_field = ENTITY_ID_FIELDS[self.entity_type]
        entity_id = getattr(self.entity, id_field)

        current_entities = getattr(draft.data, collection_name)

        if not any(
            getattr(existing, id_field) == entity_id
            for existing in current_entities
        ):
            raise ValueError(
                f"{self.entity_type} '{entity_id}' not found."
            )

        updated_entities = tuple(
            self.entity if getattr(existing, id_field) == entity_id
            else existing
            for existing in current_entities
        )

        updated_data = replace(
            draft.data,
            **{collection_name: updated_entities},
        )

        return replace(
            draft,
            data=updated_data,
            revision=draft.revision + 1,
        )


class RemoveEntityCommand:
    """Remove an existing timetable entity."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id

    def execute(self, draft: ProblemDraft) -> ProblemDraft:
        if self.entity_type not in ENTITY_COLLECTIONS:
            raise ValueError(f"Unknown entity type: {self.entity_type}")

        collection_name = ENTITY_COLLECTIONS[self.entity_type]
        id_field = ENTITY_ID_FIELDS[self.entity_type]

        current_entities = getattr(draft.data, collection_name)

        if not any(
            getattr(existing, id_field) == self.entity_id
            for existing in current_entities
        ):
            raise ValueError(
                f"{self.entity_type} '{self.entity_id}' not found."
            )

        updated_entities = tuple(
            existing
            for existing in current_entities
            if getattr(existing, id_field) != self.entity_id
        )

        updated_data = replace(
            draft.data,
            **{collection_name: updated_entities},
        )

        return replace(
            draft,
            data=updated_data,
            revision=draft.revision + 1,
        )