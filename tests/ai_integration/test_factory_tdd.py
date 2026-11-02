
"""Functional TDD tests for the Factory Method pattern."""

import unittest
from unittest.mock import Mock

from taip.ai_integration.adapters import (
    AIModelAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    ThirdModelAdapter,
)
from taip.ai_integration.creators import (
    ModelAdapterCreator,
    GeminiAdapterCreator,
    OpenAIAdapterCreator,
    ThirdModelAdapterCreator,
)


class FactoryMethodTDDTests(unittest.TestCase):

    def test_openai_creator_creates_adapter(self):
        client = Mock()

        creator = OpenAIAdapterCreator()
        creator.model_id = "openai-test"
        creator.client = client

        adapter = creator.create_adapter()

        self.assertIsInstance(adapter, OpenAIAdapter)
        self.assertEqual(
            adapter.get_model_identifier(), "openai-test"
        )
        self.assertIs(adapter.client, client)

    def test_gemini_creator_creates_adapter(self):
        client = Mock()

        creator = GeminiAdapterCreator()
        creator.model_id = "gemini-test"
        creator.client = client

        adapter = creator.create_adapter()

        self.assertIsInstance(adapter, GeminiAdapter)
        self.assertEqual(
            adapter.get_model_identifier(), "gemini-test"
        )
        self.assertIs(adapter.client, client)

    def test_third_model_creator_creates_adapter(self):
        client = Mock()

        creator = ThirdModelAdapterCreator()
        creator.model_id = "third-test"
        creator.client = client

        adapter = creator.create_adapter()

        self.assertIsInstance(adapter, ThirdModelAdapter)
        self.assertEqual(
            adapter.get_model_identifier(), "third-test"
        )
        self.assertIs(adapter.client, client)

    def test_creators_use_common_interface(self):
        creators = [
            (OpenAIAdapterCreator, "openai-test"),
            (GeminiAdapterCreator, "gemini-test"),
            (ThirdModelAdapterCreator, "third-test"),
        ]

        for creator_type, model_id in creators:
            with self.subTest(creator=creator_type.__name__):
                creator = creator_type()
                creator.model_id = model_id
                creator.client = Mock()

                self.assertIsInstance(
                    creator, ModelAdapterCreator
                )

                adapter = creator.create_adapter()

                self.assertIsInstance(
                    adapter, AIModelAdapter
                )
                self.assertEqual(
                    adapter.get_model_identifier(), model_id
                )

    def test_factory_creates_distinct_adapters(self):
        creator = OpenAIAdapterCreator()
        creator.model_id = "openai-test"
        creator.client = Mock()

        first = creator.create_adapter()
        second = creator.create_adapter()

        self.assertIsNot(first, second)
        self.assertIs(first.client, second.client)


if __name__ == "__main__":
    unittest.main()
