"""End-to-end checks for the in-memory mock application workflow."""

from datetime import timedelta
import unittest

import httpx

from taip.application.api import create_app
from taip.application.workflow import (
    MOCK_DEMO_PROMPT,
    MOCK_MODEL_IDS,
    InMemorySessionRepository,
    MockProviderFactory,
    WorkflowService,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier


class WorkflowApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        service = WorkflowService(
            InMemorySessionRepository(), MockProviderFactory(), TimetableVerifier()
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_full_mock_flow_and_independent_model_sessions(self) -> None:
        response = await self.client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})
        self.assertEqual(response.status_code, 201)
        session_id = response.json()["session_id"]

        response = await self.client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "DRAFT")

        draft_a = (await self.client.get(f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}")).json()
        draft_b = (await self.client.get(f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[1]}")).json()
        self.assertNotEqual(draft_a["draft_id"], draft_b["draft_id"])
        extracted_session = (await self.client.get(f"/api/sessions/{session_id}")).json()
        self.assertEqual(
            extracted_session["models"][MOCK_MODEL_IDS[0]]["proposal"]["raw_response"],
            "Deterministic response for the exact documented mock fixture.",
        )

        edited_data = draft_a["data"]
        edited_data["constraints"].append({
            "constraint_id": "max-day",
            "type": "max_class_periods_per_day",
            "severity": "hard",
            "parameters": {"class_id": "class-1", "maximum": 4},
            "weight": 1,
            "source_text": "Keep the daily load manageable",
        })
        edit_result = await self.client.patch(
            f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}", json={"data": edited_data}
        )
        self.assertEqual(edit_result.status_code, 200, edit_result.text)

        for action in ("confirm", "solve", "verify"):
            result = await self.client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/{action}")
            self.assertEqual(result.status_code, 200, result.text)
        summary = (await self.client.get(f"/api/sessions/{session_id}/compare")).json()
        self.assertEqual(len(summary["models"]), 3)
        model_a = next(row for row in summary["models"] if row["model_id"] == MOCK_MODEL_IDS[0])
        model_b = next(row for row in summary["models"] if row["model_id"] == MOCK_MODEL_IDS[1])
        self.assertEqual(model_a["verification_status"], "verified")
        self.assertEqual(model_a["edit_count"], 1)
        self.assertIsNone(model_b["solution_id"])
        self.assertEqual(len(summary["report"]["rows"]), 3)
        self.assertFalse(summary["report"]["snapshots_comparable"])

    async def test_mock_does_not_invent_data_for_unrecognized_prompts(self) -> None:
        response = await self.client.post("/api/sessions", json={"prompt": "Fă-mi un orar."})
        session_id = response.json()["session_id"]
        extracted = await self.client.post(
            f"/api/sessions/{session_id}/extract",
            json={"model_ids": [MOCK_MODEL_IDS[0]]},
        )
        workspace = extracted.json()["models"][MOCK_MODEL_IDS[0]]
        self.assertEqual(workspace["draft"]["data"]["classes"], [])
        self.assertEqual(workspace["proposal"]["missing_information"][0]["code"], "MOCK_FIXTURE_ONLY")
        confirmed = await self.client.post(
            f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/confirm"
        )
        self.assertEqual(confirmed.status_code, 400)

    async def test_confirmed_snapshots_are_kept_when_draft_changes(self) -> None:
        created = await self.client.post("/api/sessions", json={"prompt": MOCK_DEMO_PROMPT})
        session_id = created.json()["session_id"]
        await self.client.post(
            f"/api/sessions/{session_id}/extract", json={"model_ids": [MOCK_MODEL_IDS[0]]}
        )
        first = await self.client.post(
            f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/confirm"
        )
        first_id = first.json()["snapshot"]["snapshot_id"]
        draft = (await self.client.get(
            f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}"
        )).json()
        data = draft["data"]
        data["constraints"].append({
            "constraint_id": "max-day", "type": "max_class_periods_per_day",
            "severity": "hard", "parameters": {"class_id": "class-1", "maximum": 4},
            "weight": 1, "source_text": None,
        })
        await self.client.patch(
            f"/api/sessions/{session_id}/drafts/{MOCK_MODEL_IDS[0]}", json={"data": data}
        )
        second = await self.client.post(
            f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/confirm"
        )
        state = (await self.client.get(f"/api/sessions/{session_id}")).json()
        snapshots = state["models"][MOCK_MODEL_IDS[0]]["snapshots"]
        self.assertEqual(second.json()["snapshot"]["version"], 2)
        self.assertEqual([item["snapshot_id"] for item in snapshots], [first_id, second.json()["snapshot"]["snapshot_id"]])

    async def test_sessions_are_isolated_and_expire(self) -> None:
        repository = InMemorySessionRepository(ttl=timedelta(milliseconds=1))
        service = WorkflowService(repository, MockProviderFactory(), TimetableVerifier())
        first = service.create_session("first")
        second = service.create_session("second")
        self.assertNotEqual(first.session_id, second.session_id)

        import time
        time.sleep(0.01)
        with self.assertRaisesRegex(KeyError, "expired"):
            repository.get(first.session_id)

    async def test_session_id_is_required_to_access_workspace(self) -> None:
        response = await self.client.get("/api/sessions/not-a-session")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
