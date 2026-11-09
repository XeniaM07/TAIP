"""Offline checks for immutable, allowlisted generation telemetry."""

from dataclasses import FrozenInstanceError, asdict, fields
from copy import deepcopy
from decimal import Decimal
from importlib import import_module, util
from types import MappingProxyType
import unittest

from taip.ai_integration.config import ProviderKind


class TelemetryValueTests(unittest.TestCase):
    def setUp(self):
        module_name = "taip.ai_integration.telemetry"
        self.assertIsNotNone(
            util.find_spec(module_name), "Generation telemetry must be implemented"
        )
        self.telemetry = import_module(module_name)

    def assert_immutable_scalars(self, value):
        self.assertFalse(hasattr(value, "__dict__"))
        for field in fields(value):
            with self.subTest(field=field.name):
                self.assertIsInstance(getattr(value, field.name), (int, float, type(None)))
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, field.name, None)
                with self.assertRaises(FrozenInstanceError):
                    delattr(value, field.name)

    def test_generation_parameters_preserve_only_the_generation_controls(self):
        value = self.telemetry.GenerationParameters(0.25, 2048, True)

        self.assertEqual(asdict(value), {
            "temperature": 0.25,
            "max_output_tokens": 2048,
            "use_json_schema": True,
        })
        self.assert_immutable_scalars(value)
        self.assertEqual(value, self.telemetry.GenerationParameters(0.25, 2048, True))

    def test_usage_defaults_to_absence_and_cannot_retain_mutable_payloads(self):
        value = self.telemetry.ProviderUsage()

        self.assertEqual(asdict(value), {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "reasoning_tokens": None,
            "cached_input_tokens": None,
            "cost_usd": None,
        })
        self.assert_immutable_scalars(value)

    def test_reported_usage_is_immutable_and_hashable(self):
        value = self.telemetry.ProviderUsage(10, 20, 35, 5, 4, 0.001)

        self.assertEqual(asdict(value), {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 35,
            "reasoning_tokens": 5,
            "cached_input_tokens": 4,
            "cost_usd": 0.001,
        })
        self.assert_immutable_scalars(value)
        self.assertEqual({value, self.telemetry.ProviderUsage(10, 20, 35, 5, 4, 0.001)}, {value})


class ProviderUsageParsingTests(unittest.TestCase):
    def setUp(self):
        self.telemetry = import_module("taip.ai_integration.telemetry")
        self.parse = getattr(self.telemetry, "parse_provider_usage", None)
        self.assertTrue(callable(self.parse), "Provider usage parsing must be implemented")

    def test_missing_or_badly_shaped_optional_usage_is_absent(self):
        for provider in ProviderKind:
            with self.subTest(provider=provider, case="missing"):
                self.assertIsNone(self.parse(provider, {}))
            key = "usageMetadata" if provider is ProviderKind.GEMINI else "usage"
            for value in (None, False, 0, 0.5, "private text", [], [1], {}):
                with self.subTest(provider=provider, usage=value):
                    self.assertIsNone(self.parse(provider, {key: value}))

    def test_gemini_keeps_reported_totals_and_separate_thinking_tokens(self):
        envelope = {
            "modelVersion": "reported-model",
            "candidates": [{"content": {"parts": [{"text": "private output"}]}}],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 6,
                "totalTokenCount": 25,
                "thoughtsTokenCount": 7,
                "cachedContentTokenCount": 4,
            },
        }

        self.assertEqual(
            self.parse(ProviderKind.GEMINI, envelope),
            self.telemetry.ProviderUsage(12, 6, 25, 7, 4),
        )

    def test_groq_maps_chat_completion_counts_and_ignores_timing(self):
        envelope = {
            "usage": {
                "prompt_tokens": 18,
                "completion_tokens": 556,
                "total_tokens": 574,
                "completion_tokens_details": {"reasoning_tokens": 17},
                "prompt_tokens_details": {"cached_tokens": 12},
                "queue_time": 0.037,
                "prompt_time": 0.001,
                "completion_time": 0.463,
                "total_time": 0.464,
            },
        }

        self.assertEqual(
            self.parse(ProviderKind.GROQ, envelope),
            self.telemetry.ProviderUsage(18, 556, 574, 17, 12),
        )

    def test_openrouter_uses_charged_cost_and_preserves_reported_zeros(self):
        envelope = {
            "usage": {
                "prompt_tokens": 194,
                "completion_tokens": 2,
                "total_tokens": 196,
                "completion_tokens_details": {"reasoning_tokens": 0},
                "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 100},
                "cost": 0.95,
                "cost_details": {"upstream_inference_cost": 19},
            },
        }

        self.assertEqual(
            self.parse(ProviderKind.OPENROUTER, envelope),
            self.telemetry.ProviderUsage(194, 2, 196, 0, 0, 0.95),
        )

    def test_partial_usage_leaves_missing_fields_unknown_without_recomputation(self):
        examples = (
            (ProviderKind.GEMINI, {"usageMetadata": {"thoughtsTokenCount": 7}},
             self.telemetry.ProviderUsage(reasoning_tokens=7)),
            (ProviderKind.GEMINI,
             {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}},
             self.telemetry.ProviderUsage(input_tokens=10, output_tokens=3)),
            (ProviderKind.GROQ, {"usage": {"prompt_tokens": 10, "completion_tokens": 3}},
             self.telemetry.ProviderUsage(input_tokens=10, output_tokens=3)),
            (ProviderKind.OPENROUTER,
             {"usage": {"total_tokens": 11, "completion_tokens_details": {"reasoning_tokens": 2}}},
             self.telemetry.ProviderUsage(total_tokens=11, reasoning_tokens=2)),
        )
        for provider, envelope, expected in examples:
            with self.subTest(provider=provider, envelope=envelope):
                self.assertEqual(self.parse(provider, envelope), expected)

    def test_bad_nested_details_do_not_discard_valid_sibling_counts(self):
        for provider in (ProviderKind.GROQ, ProviderKind.OPENROUTER):
            for bad in (None, False, 4, 0.5, "private text", [], [1]):
                with self.subTest(provider=provider, details=bad):
                    envelope = {"usage": {
                        "prompt_tokens": 8,
                        "completion_tokens_details": bad,
                        "prompt_tokens_details": bad,
                    }}
                    self.assertEqual(
                        self.parse(provider, envelope), self.telemetry.ProviderUsage(input_tokens=8)
                    )

    def test_each_token_count_requires_an_exact_nonnegative_integer(self):
        class IntegerSubclass(int):
            pass

        invalid_values = (
            None, True, False, -1, -0.5, 0.0, 1.0, "0", "12",
            float("nan"), float("inf"), -float("inf"), {}, [],
            Decimal("1"), IntegerSubclass(1),
        )
        provider_fields = {
            ProviderKind.GEMINI: (
                ("promptTokenCount",), ("candidatesTokenCount",), ("totalTokenCount",),
                ("thoughtsTokenCount",), ("cachedContentTokenCount",),
            ),
            ProviderKind.GROQ: (
                ("prompt_tokens",), ("completion_tokens",), ("total_tokens",),
                ("completion_tokens_details", "reasoning_tokens"),
                ("prompt_tokens_details", "cached_tokens"),
            ),
            ProviderKind.OPENROUTER: (
                ("prompt_tokens",), ("completion_tokens",), ("total_tokens",),
                ("completion_tokens_details", "reasoning_tokens"),
                ("prompt_tokens_details", "cached_tokens"),
            ),
        }
        for provider, paths in provider_fields.items():
            key = "usageMetadata" if provider is ProviderKind.GEMINI else "usage"
            for path in paths:
                for bad in invalid_values:
                    with self.subTest(provider=provider, path=path, value=bad):
                        usage = {path[0]: bad if len(path) == 1 else {path[1]: bad}}
                        self.assertIsNone(self.parse(provider, {key: usage}))

    def test_bad_values_are_dropped_individually(self):
        self.assertEqual(
            self.parse(ProviderKind.GEMINI, {"usageMetadata": {
                "promptTokenCount": True,
                "candidatesTokenCount": 6,
                "totalTokenCount": "18",
                "thoughtsTokenCount": -1,
                "cachedContentTokenCount": 0,
            }}),
            self.telemetry.ProviderUsage(output_tokens=6, cached_input_tokens=0),
        )
        for provider in (ProviderKind.GROQ, ProviderKind.OPENROUTER):
            with self.subTest(provider=provider):
                self.assertEqual(
                    self.parse(provider, {"usage": {
                        "prompt_tokens": False,
                        "completion_tokens": 6,
                        "total_tokens": "18",
                        "completion_tokens_details": {"reasoning_tokens": float("inf")},
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "cost": "0.01",
                    }}),
                    self.telemetry.ProviderUsage(output_tokens=6, cached_input_tokens=0),
                )

    def test_zero_and_large_counts_are_preserved_exactly(self):
        for count in (0, 10**400):
            examples = (
                (ProviderKind.GEMINI, {"usageMetadata": {
                    "promptTokenCount": count, "candidatesTokenCount": count,
                    "totalTokenCount": count, "thoughtsTokenCount": count,
                    "cachedContentTokenCount": count,
                }}),
                (ProviderKind.GROQ, {"usage": {
                    "prompt_tokens": count, "completion_tokens": count, "total_tokens": count,
                    "completion_tokens_details": {"reasoning_tokens": count},
                    "prompt_tokens_details": {"cached_tokens": count},
                }}),
                (ProviderKind.OPENROUTER, {"usage": {
                    "prompt_tokens": count, "completion_tokens": count, "total_tokens": count,
                    "completion_tokens_details": {"reasoning_tokens": count},
                    "prompt_tokens_details": {"cached_tokens": count},
                }}),
            )
            for provider, envelope in examples:
                with self.subTest(provider=provider, count=count):
                    self.assertEqual(
                        self.parse(provider, envelope),
                        self.telemetry.ProviderUsage(count, count, count, count, count),
                    )

    def test_reported_cost_accepts_only_finite_nonnegative_numbers(self):
        for reported, expected in ((0, 0.0), (2, 2.0), (0.125, 0.125), (-0.0, 0.0)):
            with self.subTest(cost=reported):
                result = self.parse(ProviderKind.OPENROUTER, {"usage": {"cost": reported}})
                self.assertEqual(result, self.telemetry.ProviderUsage(cost_usd=expected))
                self.assertIs(type(result.cost_usd), float)
        for bad in (
            None, True, False, -1, -0.01, "0.125", float("nan"), float("inf"),
            -float("inf"), 10**400, {}, [], Decimal("0.125"),
        ):
            with self.subTest(cost=bad):
                self.assertIsNone(self.parse(ProviderKind.OPENROUTER, {"usage": {"cost": bad}}))
                self.assertEqual(
                    self.parse(ProviderKind.OPENROUTER, {"usage": {"cost": bad, "prompt_tokens": 8}}),
                    self.telemetry.ProviderUsage(input_tokens=8),
                )

    def test_cost_is_never_estimated_or_replaced_by_upstream_cost(self):
        for usage in (
            {"cost_details": {"upstream_inference_cost": 0.01}},
            {"cost": None, "cost_details": {"upstream_inference_cost": 0.01}},
            {"upstream_inference_cost": 0.01},
            {"pricing": {"prompt": "0.0001", "completion": "0.0002"}},
        ):
            with self.subTest(usage=usage):
                self.assertIsNone(self.parse(ProviderKind.OPENROUTER, {"usage": usage}))
        self.assertIsNone(self.parse(ProviderKind.OPENROUTER, {"cost": 0.01}))
        self.assertEqual(
            self.parse(ProviderKind.OPENROUTER, {"usage": {
                "prompt_tokens": 1000, "completion_tokens": 2000,
                "pricing": {"prompt": "0.0001", "completion": "0.0002"},
            }}),
            self.telemetry.ProviderUsage(input_tokens=1000, output_tokens=2000),
        )

    def test_gemini_and_groq_do_not_interpret_undocumented_cost_fields(self):
        for provider, key in ((ProviderKind.GEMINI, "usageMetadata"), (ProviderKind.GROQ, "usage")):
            with self.subTest(provider=provider):
                self.assertIsNone(self.parse(provider, {key: {"cost": 0.01, "cost_usd": 0.02}}))

    def test_unknown_extras_and_response_text_are_never_retained(self):
        envelope = {
            "error": {"message": "private diagnostic"},
            "messages": [{"content": "private prompt"}],
            "choices": [{"message": {"content": "private response"}}],
            "usage": {
                "prompt_tokens": 3,
                "reasoning": "private narrative",
                "output_tokens": 99,
                "cost_usd": 999,
                "completion_tokens_details": {"reasoning": "private thoughts"},
            },
        }
        result = self.parse(ProviderKind.OPENROUTER, envelope)

        self.assertEqual(result, self.telemetry.ProviderUsage(input_tokens=3))
        self.assertNotIn("private", repr(result))
        self.assertNotIn("messages", asdict(result))

    def test_mapping_inputs_are_supported_without_mutation_or_shared_state(self):
        envelope = {"usage": {
            "prompt_tokens": 7,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }}
        original = deepcopy(envelope)
        first = self.parse(ProviderKind.GROQ, envelope)
        self.assertEqual(envelope, original)
        envelope["usage"]["prompt_tokens"] = 100
        envelope["usage"]["completion_tokens_details"]["reasoning_tokens"] = 20
        self.assertEqual(first, self.telemetry.ProviderUsage(input_tokens=7, reasoning_tokens=2))
        self.assertIsNone(self.parse(ProviderKind.GROQ, {}))
        self.assertEqual(
            self.parse(ProviderKind.OPENROUTER, MappingProxyType({"usage": MappingProxyType({
                "prompt_tokens": 3,
                "prompt_tokens_details": MappingProxyType({"cached_tokens": 1}),
                "completion_tokens_details": MappingProxyType({"reasoning_tokens": 2}),
            })})),
            self.telemetry.ProviderUsage(input_tokens=3, reasoning_tokens=2, cached_input_tokens=1),
        )
