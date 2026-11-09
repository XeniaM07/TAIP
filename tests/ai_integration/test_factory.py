"""Factory Method checks with injected transport and no network access."""

from importlib import import_module, util
import unittest

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIConfigurationError
from taip.common.timetable_protocols import AIProviderFactoryPort, AIProviderPort


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def post(self, **request):
        self.calls.append(request)
        raise AssertionError("Constructing or retrieving adapters must not perform HTTP calls")


def configuration(kind, model_id=None):
    return ProviderConfig(
        model_id=model_id or kind.value,
        provider=kind,
        api_model_id="vendor/exact-" + kind.value,
        api_key="fake-secret-" + kind.value,
    )


class ProviderFactoryTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.factory"
        self.assertIsNotNone(util.find_spec(name), "The timetable provider factory must exist")
        self.factory = import_module(name)
        self.providers = import_module("taip.ai_integration.providers")
        self.transport = RecordingTransport()

    def creator_pairs(self):
        return (
            (ProviderKind.GEMINI, self.factory.GeminiTimetableAdapterCreator, self.providers.GeminiTimetableAdapter),
            (ProviderKind.GROQ, self.factory.GroqTimetableAdapterCreator, self.providers.GroqTimetableAdapter),
            (ProviderKind.OPENROUTER, self.factory.OpenRouterTimetableAdapterCreator, self.providers.OpenRouterTimetableAdapter),
        )

    def test_abstract_creator_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            self.factory.TimetableAdapterCreator(configuration(ProviderKind.GEMINI), self.transport)

    def test_concrete_creators_build_the_corresponding_canonical_adapters(self):
        for kind, creator_type, adapter_type in self.creator_pairs():
            with self.subTest(provider=kind):
                self.assertTrue(issubclass(creator_type, self.factory.TimetableAdapterCreator))
                creator = creator_type(configuration(kind, "logical-" + kind.value), self.transport)
                adapter = creator.create_adapter()
                self.assertIsInstance(adapter, adapter_type)
                self.assertIsInstance(adapter, AIProviderPort)
                self.assertEqual(adapter.model_id, "logical-" + kind.value)
        self.assertEqual(self.transport.calls, [])

    def test_creators_reject_a_configuration_for_another_provider(self):
        for kind, creator_type, _ in self.creator_pairs():
            other = ProviderKind.GROQ if kind is ProviderKind.GEMINI else ProviderKind.GEMINI
            with self.subTest(provider=kind):
                with self.assertRaises(AIConfigurationError) as caught:
                    creator_type(configuration(other), self.transport)
                self.assertEqual(caught.exception.field, "provider")

    def test_creators_reject_nonconfiguration_values_safely(self):
        for _, creator_type, _ in self.creator_pairs():
            for value in (None, {}, "fake-secret-input", 42):
                with self.subTest(creator=creator_type.__name__, value_type=type(value).__name__):
                    with self.assertRaises(AIConfigurationError) as caught:
                        creator_type(value, self.transport)
                    self.assertEqual(caught.exception.field, "config")
                    self.assertNotIn("fake-secret-input", str(caught.exception))

    def test_factory_exposes_all_configured_models_and_the_canonical_factory_port(self):
        factory = self.factory.AIProviderFactory(
            tuple(configuration(kind) for kind in ProviderKind), transport=self.transport,
        )
        self.assertIsInstance(factory, AIProviderFactoryPort)
        self.assertEqual(factory.MODEL_IDS, ("gemini", "groq", "openrouter"))
        for kind, _, adapter_type in self.creator_pairs():
            with self.subTest(provider=kind):
                adapter = factory.get_provider(kind.value)
                self.assertIsInstance(adapter, adapter_type)
                self.assertEqual(adapter.model_id, kind.value)
        self.assertEqual(self.transport.calls, [])

    def test_get_provider_returns_the_same_eagerly_built_adapter(self):
        factory = self.factory.AIProviderFactory(
            (configuration(ProviderKind.GROQ),), transport=self.transport,
        )
        self.assertIs(factory.get_provider("groq"), factory.get_provider("groq"))
        self.assertEqual(self.transport.calls, [])

    def test_model_identifiers_are_readonly(self):
        factory = self.factory.AIProviderFactory(
            (configuration(ProviderKind.GEMINI),), transport=self.transport,
        )
        with self.assertRaises(AttributeError):
            factory.MODEL_IDS = ("other",)
        self.assertEqual(factory.MODEL_IDS, ("gemini",))

    def test_factory_keeps_distinct_aliases_for_models_using_the_same_provider(self):
        factory = self.factory.AIProviderFactory(
            (configuration(ProviderKind.GROQ, "llama-a"), configuration(ProviderKind.GROQ, "llama-b")),
            transport=self.transport,
        )
        self.assertEqual(factory.MODEL_IDS, ("llama-a", "llama-b"))
        self.assertIsNot(factory.get_provider("llama-a"), factory.get_provider("llama-b"))
        self.assertEqual(factory.get_provider("llama-a").model_id, "llama-a")
        self.assertEqual(factory.get_provider("llama-b").model_id, "llama-b")

    def test_factory_rejects_duplicate_aliases_without_silent_replacement(self):
        for other_kind in (ProviderKind.GEMINI, ProviderKind.GROQ):
            with self.subTest(second_provider=other_kind):
                with self.assertRaises(AIConfigurationError) as caught:
                    self.factory.AIProviderFactory(
                        (configuration(ProviderKind.GEMINI, "same"), configuration(other_kind, "same")),
                        transport=self.transport,
                    )
                self.assertEqual(caught.exception.field, "model_id")
        self.assertEqual(self.transport.calls, [])

    def test_factory_rejects_empty_and_noniterable_configuration_sources(self):
        for source in ((), [], iter(()), None, 42):
            with self.subTest(source_type=type(source).__name__):
                with self.assertRaises(AIConfigurationError) as caught:
                    self.factory.AIProviderFactory(source, transport=self.transport)
                self.assertEqual(caught.exception.field, "providers")

    def test_factory_rejects_invalid_configuration_elements_safely(self):
        for value in (None, {}, "fake-secret-input", 42):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(AIConfigurationError) as caught:
                    self.factory.AIProviderFactory((value,), transport=self.transport)
                self.assertEqual(caught.exception.field, "config")
                self.assertNotIn("fake-secret-input", str(caught.exception))

    def test_unknown_model_is_reported_without_fallback_or_echoing_input(self):
        factory = self.factory.AIProviderFactory(
            (configuration(ProviderKind.GEMINI),), transport=self.transport,
        )
        with self.assertRaises(KeyError) as caught:
            factory.get_provider("fake-secret-unknown-model")
        self.assertNotIn("fake-secret-unknown-model", str(caught.exception))
        self.assertEqual(self.transport.calls, [])

    def test_factory_copies_input_members_and_consumes_a_single_pass_iterable(self):
        configs = [configuration(ProviderKind.OPENROUTER)]
        factory = self.factory.AIProviderFactory(iter(configs), transport=self.transport)
        configs.clear()
        self.assertEqual(factory.MODEL_IDS, ("openrouter",))
        self.assertEqual(factory.get_provider("openrouter").model_id, "openrouter")


if __name__ == "__main__":
    unittest.main()
