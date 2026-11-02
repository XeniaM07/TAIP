
"""Functional TDD tests for AI provider adapters."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from taip.ai_integration.adapters import (
    OpenAIAdapter,
    GeminiAdapter,
    ThirdModelAdapter,
)
from taip.common.contracts import (
    Constraint,
    ModelRequest,
    ModelStatus,
    ProblemSnapshot,
)


class AdapterTDDTests(unittest.TestCase):

    def setUp(self):
        self.snapshot = ProblemSnapshot(
            problem_id="problem-1",
            version=1,
            original_prompt="Solve x + 2 = 5",
            objective="Find x",
            constraints=(Constraint("c1", "x >= 0"),),
        )

    def test_openai_adapter_normalizes_response(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            output_text="x = 3"
        )

        adapter = OpenAIAdapter()
        adapter.model_id = "openai-test"
        adapter.client = client

        request = ModelRequest("openai-test", self.snapshot)
        response = adapter.generate_solution(
            request, run_id="run-1"
        )

        self.assertEqual(response.model_id, "openai-test")
        self.assertEqual(response.run_id, "run-1")
        self.assertEqual(response.output, "x = 3")
        self.assertEqual(response.status, ModelStatus.COMPLETED)

        client.responses.create.assert_called_once()
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "openai-test")
        self.assertIn("Solve x + 2 = 5", kwargs["input"])
        self.assertIn("x >= 0", kwargs["input"])

    def test_gemini_adapter_normalizes_response(self):
        client = Mock()
        client.models.generate_content.return_value = (
            SimpleNamespace(text="x = 3")
        )

        adapter = GeminiAdapter()
        adapter.model_id = "gemini-test"
        adapter.client = client

        request = ModelRequest("gemini-test", self.snapshot)
        response = adapter.generate_solution(
            request, run_id="run-2"
        )

        self.assertEqual(response.output, "x = 3")
        self.assertEqual(response.status, ModelStatus.COMPLETED)
        self.assertEqual(response.run_id, "run-2")

        client.models.generate_content.assert_called_once()

    def test_third_model_adapter_normalizes_response(self):
        client = Mock()
        client.generate.return_value = "x = 3"

        adapter = ThirdModelAdapter()
        adapter.model_id = "third-test"
        adapter.client = client

        request = ModelRequest("third-test", self.snapshot)
        response = adapter.generate_solution(
            request, run_id="run-3"
        )

        self.assertEqual(response.output, "x = 3")
        self.assertEqual(response.status, ModelStatus.COMPLETED)

        client.generate.assert_called_once()

    def test_adapter_returns_configured_identifier(self):
        adapter = OpenAIAdapter()
        adapter.model_id = "openai-test"

        self.assertEqual(
            adapter.get_model_identifier(),
            "openai-test",
        )


if __name__ == "__main__":
    unittest.main()
