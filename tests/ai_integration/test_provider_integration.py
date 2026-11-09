"""Exercise factory, real adapters, HTTPX, codecs and independent verification."""

import json
import unittest

import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.common.timetable_contracts import (
    OriginalProblem, TimetableProblemSnapshot, VerificationStatus,
)
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from tests.ai_integration.test_mock_provider import (
    EXTRACTION_RESPONSE, INVALID_SOLUTION_RESPONSE, PROMPT, SOLUTION_RESPONSE, timetable_data,
)
from tests.ai_integration.test_providers import completion


class ProviderIntegrationTests(unittest.TestCase):
    def run_roundtrip(self, kind, solution_text):
        envelopes = [completion(kind, EXTRACTION_RESPONSE), completion(kind, solution_text)]
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=envelopes.pop(0))

        config = ProviderConfig(kind.value, kind, "configured-exact-model", "fake-api-key")
        with HTTPJSONTransport(transport=httpx.MockTransport(handler)) as transport:
            factory = AIProviderFactory((config,), transport=transport)
            provider = factory.get_provider(kind.value)
            proposal = provider.extract(OriginalProblem("problem-a", PROMPT))
            self.assertEqual(proposal.data, timetable_data())
            snapshot = TimetableProblemSnapshot(
                "snapshot-a", "draft-a", proposal.problem_id, provider.model_id,
                1, PROMPT, proposal.data,
            )
            solution = provider.solve(snapshot)

        self.assertEqual(len(requests), 2)
        self.assertEqual(envelopes, [])
        self.assertEqual(solution.snapshot_id, "snapshot-a")
        self.assertEqual(solution.model_id, kind.value)
        return solution, TimetableVerifier().verify(snapshot, solution)

    def test_each_real_adapter_roundtrips_romanian_data_to_a_verified_candidate_offline(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                solution, verification = self.run_roundtrip(kind, SOLUTION_RESPONSE)
                self.assertEqual(len(solution.assignments), 2)
                self.assertEqual(verification.status, VerificationStatus.VERIFIED)

    def test_each_real_adapter_preserves_invalid_candidate_for_independent_rejection(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                solution, verification = self.run_roundtrip(kind, INVALID_SOLUTION_RESPONSE)
                self.assertEqual([lesson.slot_id for lesson in solution.assignments], ["mon-1", "mon-1"])
                self.assertEqual(verification.status, VerificationStatus.FAILED)
                self.assertIn("schedule.teacher_overlap", {issue.code for issue in verification.violations})


if __name__ == "__main__":
    unittest.main()
