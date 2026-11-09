"""Provider bounds also protect retained/public operation metadata."""

import unittest

from taip.ai_integration.config import ProviderConfig, ProviderKind, load_provider_configs
from taip.ai_integration.errors import AIConfigurationError, AIProviderError
from taip.ai_integration.providers import GroqTimetableAdapter
from taip.common.timetable_contracts import OriginalProblem
from tests.ai_integration.test_providers import RecordingTransport, completion
from tests.ai_integration.test_config import provider_environment


class ProviderResourceCapsTests(unittest.TestCase):
    def config(self, **values):
        return ProviderConfig(**dict(model_id="groq", provider=ProviderKind.GROQ,
                                     api_model_id="exact-model", api_key="synthetic-key") | values)

    def test_response_and_identifier_configuration_have_supported_upper_bounds(self):
        for values in ({"max_response_bytes": 1048577}, {"model_id": "x" * 129},
                       {"api_model_id": "x" * 513}, {"api_key": "x" * 4097},
                       {"api_key": "key\nheader"}):
            with self.subTest(fields=list(values)), self.assertRaises(AIConfigurationError):
                self.config(**values)

    def test_oversized_reported_model_is_rejected_before_metadata_capture(self):
        reply = completion(ProviderKind.GROQ)
        reply["model"] = "x" * 10000
        records = []
        adapter = GroqTimetableAdapter(self.config(), RecordingTransport(reply), observer=records.append)
        with self.assertRaises(AIProviderError) as error:
            adapter.extract(OriginalProblem("p", "A bounded school timetable"))
        self.assertEqual(error.exception.code, "invalid_envelope")
        self.assertIsNone(records[0].returned_model_id)

    def test_normal_reported_model_is_preserved_exactly(self):
        records = []
        adapter = GroqTimetableAdapter(self.config(), RecordingTransport(completion(ProviderKind.GROQ)), observer=records.append)
        adapter.extract(OriginalProblem("p", "A bounded school timetable"))
        self.assertEqual(records[0].returned_model_id, "returned-model-version")

    def test_environment_cap_failures_keep_safe_configuration_field_names(self):
        for name, value in (("GROQ_MODEL", "x" * 513), ("GROQ_API_KEY", "x" * 4097),
                            ("GROQ_MODEL", "\ud800")):
            with self.subTest(name=name), self.assertRaises(AIConfigurationError) as caught:
                load_provider_configs(provider_environment() | {name: value})
            self.assertEqual(caught.exception.field, name)
