"""End-to-end checks for the in-memory mock application workflow."""

from datetime import timedelta
import unittest

import httpx

from taip.application.api import create_app
from taip.application.workflow import InMemorySessionRepository, MockProviderFactory, WorkflowService
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
        response = await self.client.post("/api/sessions", json={"prompt": "Make a small math timetable"})
        self.assertEqual(response.status_code, 201)
        session_id = response.json()["session_id"]

        response = await self.client.post(f"/api/sessions/{session_id}/extract", json={"model_ids": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "DRAFT")

        draft_a = (await self.client.get(f"/api/sessions/{session_id}/drafts/model-a")).json()
        draft_b = (await self.client.get(f"/api/sessions/{session_id}/drafts/model-b")).json()
        self.assertNotEqual(draft_a["draft_id"], draft_b["draft_id"])

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
            f"/api/sessions/{session_id}/drafts/model-a", json={"data": edited_data}
        )
        self.assertEqual(edit_result.status_code, 200, edit_result.text)

        for action in ("confirm", "solve", "verify"):
            result = await self.client.post(f"/api/sessions/{session_id}/models/model-a/{action}")
            self.assertEqual(result.status_code, 200, result.text)
        summary = (await self.client.get(f"/api/sessions/{session_id}/compare")).json()
        self.assertEqual(len(summary["models"]), 3)
        model_a = next(row for row in summary["models"] if row["model_id"] == "model-a")
        model_b = next(row for row in summary["models"] if row["model_id"] == "model-b")
        self.assertEqual(model_a["verification_status"], "verified")
        self.assertEqual(model_a["edit_count"], 1)
        self.assertIsNone(model_b["solution_id"])

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
