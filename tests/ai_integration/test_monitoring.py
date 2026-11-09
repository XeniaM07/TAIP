"""Invocation monitoring observes calls without changing provider behavior."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from importlib import import_module, util
import inspect
import logging
from threading import Barrier
import unittest
from unittest.mock import patch

from taip.ai_integration.errors import (
    AIConfigurationError,
    AIProviderError,
    AIResponseError,
)


class CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        module_name = "taip.ai_integration.monitoring"
        self.assertIsNotNone(
            util.find_spec(module_name), "Invocation monitoring must be implemented"
        )
        self.monitoring = import_module(module_name)
        self.logger = logging.getLogger(module_name)
        self.handler = CollectingHandler()
        previous_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.setLevel, previous_level)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def provider(self, *, action=None, observer=None):
        monitor = self.monitoring.monitor_ai_call

        class Provider:
            def __init__(self):
                self.calls = []

            @property
            def model_id(self):
                return "logical-groq"

            @property
            def provider_name(self):
                return "groq"

            @property
            def requested_model_id(self):
                return "vendor/exact-model:free"

            @property
            def invocation_observer(self):
                return observer

            @monitor("extract")
            def extract(self, problem, *, option=None):
                """Extract a provider result for the original arguments."""
                self.calls.append(("extract", problem, option))
                return action(self, problem, option) if action else problem

            @monitor("solve")
            def solve(self, problem):
                self.calls.append(("solve", problem))
                return action(self, problem, None) if action else problem

        return Provider()

    def events(self):
        return [r for r in self.handler.records if r.getMessage() == "ai.invocation"]

    def test_success_preserves_identity_arguments_and_measures_duration(self):
        records = []
        result = object()
        option = object()
        provider = self.provider(observer=records.append)
        with patch.object(self.monitoring, "perf_counter_ns", side_effect=[1_000_000, 4_250_000]):
            returned = provider.extract(result, option=option)

        self.assertIs(returned, result)
        self.assertEqual(provider.calls, [("extract", result, option)])
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsInstance(record, self.monitoring.InvocationRecord)
        self.assertIs(record.result, result)
        self.assertEqual(record.duration_ms, 3.25)
        self.assertEqual(record.operation, "extract")
        self.assertEqual(record.provider, "groq")
        self.assertEqual(record.model_id, "logical-groq")
        self.assertEqual(record.requested_model_id, "vendor/exact-model:free")
        self.assertEqual(record.outcome, "success")
        self.assertIsNone(record.error_code)
        self.assertIsNone(record.http_status_code)
        self.assertIsNone(record.retry_after_seconds)
        self.assertIsNone(record.raw_response)
        self.assertIsNone(record.returned_model_id)
        self.assertIsNone(record.upstream_provider)
        self.assertTrue(record.call_id)

    def test_decorator_preserves_method_signature_name_and_documentation(self):
        provider = self.provider()
        operation = type(provider).extract
        self.assertEqual(operation.__name__, "extract")
        self.assertEqual(operation.__doc__, "Extract a provider result for the original arguments.")
        self.assertEqual(str(inspect.signature(operation)), "(self, problem, *, option=None)")
        self.assertTrue(hasattr(operation, "__wrapped__"))

    def test_capture_updates_only_provided_fields_and_is_local_to_one_invocation(self):
        records = []

        def action(provider, problem, option):
            if problem == "capture":
                self.monitoring.capture_provider_response(
                    returned_model_id="vendor/served-revision", upstream_provider="upstream-a"
                )
                self.monitoring.capture_provider_response(raw_response="private-response")
                self.monitoring.capture_provider_response(returned_model_id=None, raw_response=None)
            return problem

        self.monitoring.capture_provider_response(raw_response="outside-before")
        provider = self.provider(action=action, observer=records.append)
        provider.solve("capture")
        self.monitoring.capture_provider_response(raw_response="outside-after")
        provider.solve("next")
        self.assertEqual(records[0].returned_model_id, "vendor/served-revision")
        self.assertEqual(records[0].upstream_provider, "upstream-a")
        self.assertEqual(records[0].raw_response, "private-response")
        self.assertIsNone(records[1].raw_response)
        self.assertIsNone(records[1].returned_model_id)
        self.assertIsNone(records[1].upstream_provider)
        self.assertNotEqual(records[0].call_id, records[1].call_id)

    def test_provider_error_records_safe_typed_metadata_and_reraises_same_exception(self):
        records = []
        error = AIProviderError("rate_limit", status_code=429, retry_after_seconds=3.5)

        def fail(provider, problem, option):
            raise error

        provider = self.provider(action=fail, observer=records.append)
        with self.assertRaises(AIProviderError) as caught:
            provider.solve("private-prompt")
        self.assertIs(caught.exception, error)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(records[0].outcome, "error")
        self.assertEqual(records[0].error_code, "rate_limit")
        self.assertEqual(records[0].http_status_code, 429)
        self.assertEqual(records[0].retry_after_seconds, 3.5)
        self.assertIsNone(records[0].result)

    def test_response_and_configuration_errors_record_codes_with_raw_fallback(self):
        malformed = AIResponseError("invalid_json", "$", "safe explanation")
        malformed.raw_response = "private-malformed-response"
        configuration = AIConfigurationError("model")
        for error, expected_code, expected_raw in (
            (malformed, "invalid_json", "private-malformed-response"),
            (configuration, "configuration_error", None),
        ):
            with self.subTest(code=expected_code):
                records = []

                def fail(provider, problem, option):
                    raise error

                provider = self.provider(action=fail, observer=records.append)
                with self.assertRaises(type(error)) as caught:
                    provider.extract("private-prompt")
                self.assertIs(caught.exception, error)
                self.assertEqual(records[0].error_code, expected_code)
                self.assertEqual(records[0].raw_response, expected_raw)
                self.assertIsNone(records[0].http_status_code)

    def test_captured_raw_text_takes_precedence_over_response_error_fallback(self):
        records = []
        error = AIResponseError("invalid_json", "$", "safe explanation")
        error.raw_response = "fallback-response"

        def fail(provider, problem, option):
            self.monitoring.capture_provider_response(raw_response="captured-response")
            raise error

        with self.assertRaises(AIResponseError):
            self.provider(action=fail, observer=records.append).solve("input")
        self.assertEqual(records[0].raw_response, "captured-response")

    def test_record_and_structured_log_exclude_private_input_result_and_raw_text(self):
        records = []
        private_prompt = "secret-user-prompt"
        private_raw = "secret-provider-body"
        private_result = {"value": "secret-parsed-result"}

        def action(provider, problem, option):
            self.monitoring.capture_provider_response(
                raw_response=private_raw,
                returned_model_id="exact/served-model",
                upstream_provider="upstream-a",
            )
            return private_result

        provider = self.provider(action=action, observer=records.append)
        self.assertIs(provider.extract(private_prompt), private_result)
        record = records[0]
        self.assertIs(record.result, private_result)
        self.assertEqual(record.raw_response, private_raw)
        with self.assertRaises(FrozenInstanceError):
            record.outcome = "changed"
        self.assertFalse(hasattr(record, "__dict__"))
        self.assertEqual(len(self.events()), 1)
        log = self.events()[0]
        expected = {
            "ai_call_id": record.call_id,
            "ai_operation": "extract",
            "ai_provider": "groq",
            "ai_model_id": "logical-groq",
            "ai_requested_model_id": "vendor/exact-model:free",
            "ai_returned_model_id": "exact/served-model",
            "ai_upstream_provider": "upstream-a",
            "ai_duration_ms": record.duration_ms,
            "ai_outcome": "success",
            "ai_error_code": None,
            "ai_http_status_code": None,
            "ai_retry_after_seconds": None,
        }
        self.assertEqual({k: v for k, v in vars(log).items() if k.startswith("ai_")}, expected)
        for private_value in (private_prompt, private_raw, "secret-parsed-result"):
            self.assertNotIn(private_value, repr(record))
            self.assertNotIn(private_value, repr(vars(log)))
        self.assertIsNone(log.exc_info)
        self.assertIsNone(log.exc_text)

    def test_unexpected_error_never_formats_or_logs_the_exception(self):
        records = []

        class PrivateError(RuntimeError):
            def __str__(self):
                raise AssertionError("Monitoring must never format the original exception")

        error = PrivateError("secret-exception-body")

        def fail(provider, problem, option):
            raise error

        with self.assertRaises(PrivateError) as caught:
            self.provider(action=fail, observer=records.append).solve("secret-prompt")
        self.assertIs(caught.exception, error)
        self.assertEqual(records[0].error_code, "unexpected_error")
        self.assertNotIn("secret-exception-body", repr(records[0]))
        for log in self.handler.records:
            self.assertNotIn("secret-exception-body", repr(vars(log)))
            self.assertNotIn("secret-prompt", repr(vars(log)))
            self.assertIsNone(log.exc_info)

    def test_process_interrupt_is_observed_and_reraised_by_identity(self):
        for error in (KeyboardInterrupt("private-interrupt"), SystemExit("private-exit")):
            with self.subTest(kind=type(error).__name__):
                records = []

                def fail(provider, problem, option):
                    raise error

                with self.assertRaises(type(error)) as caught:
                    self.provider(action=fail, observer=records.append).solve("input")
                self.assertIs(caught.exception, error)
                self.assertEqual(records[0].outcome, "error")
                self.assertEqual(records[0].error_code, "unexpected_error")

    def test_observer_failure_uses_fixed_warning_and_preserves_success_and_failure(self):
        def observer(record):
            raise RuntimeError("secret-observer-failure")

        result = object()
        self.assertIs(self.provider(observer=observer).solve(result), result)
        error = ValueError("secret-original-error")

        def fail(provider, problem, option):
            raise error

        with self.assertRaises(ValueError) as caught:
            self.provider(action=fail, observer=observer).extract("input")
        self.assertIs(caught.exception, error)
        warnings = [r for r in self.handler.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[0].getMessage(), warnings[1].getMessage())
        for log in self.handler.records:
            self.assertNotIn("secret-observer-failure", repr(vars(log)))
            self.assertNotIn("secret-original-error", repr(vars(log)))
            self.assertIsNone(log.exc_info)

    def test_logging_handler_failure_does_not_mask_result_or_original_error(self):
        class BrokenHandler(logging.Handler):
            def emit(self, record):
                raise RuntimeError("secret-handler-error")

        broken = BrokenHandler()
        self.logger.addHandler(broken)
        self.addCleanup(self.logger.removeHandler, broken)
        records = []
        result = object()
        self.assertIs(self.provider(observer=records.append).solve(result), result)
        error = ValueError("private-original")

        def fail(provider, problem, option):
            raise error

        with self.assertRaises(ValueError) as caught:
            self.provider(action=fail, observer=records.append).solve("input")
        self.assertIs(caught.exception, error)
        self.assertEqual([record.outcome for record in records], ["success", "error"])

    def test_no_observer_does_not_retain_invocation_on_the_provider(self):
        provider = self.provider()
        initial_fields = set(vars(provider))
        self.assertEqual(provider.solve("result"), "result")
        self.assertEqual(set(vars(provider)), initial_fields)
        self.assertEqual(len(self.events()), 1)

    def test_nested_calls_restore_outer_capture_and_observers_cannot_contaminate_it(self):
        records = []

        def observer(record):
            records.append(record)
            self.monitoring.capture_provider_response(
                raw_response="observer-taint", returned_model_id="observer-model"
            )

        def action(provider, problem, option):
            if problem == "outer":
                self.monitoring.capture_provider_response(
                    raw_response="outer-before", returned_model_id="outer-model"
                )
                self.assertEqual(provider.extract("inner"), "inner-result")
                self.monitoring.capture_provider_response(raw_response="outer-after")
                return "outer-result"
            self.monitoring.capture_provider_response(
                raw_response="inner-raw", returned_model_id="inner-model"
            )
            return "inner-result"

        provider = self.provider(action=action, observer=observer)
        self.assertEqual(provider.solve("outer"), "outer-result")
        self.assertEqual([r.operation for r in records], ["extract", "solve"])
        self.assertEqual(records[0].raw_response, "inner-raw")
        self.assertEqual(records[0].returned_model_id, "inner-model")
        self.assertEqual(records[1].raw_response, "outer-after")
        self.assertEqual(records[1].returned_model_id, "outer-model")

    def test_nested_error_restores_outer_context_when_provider_catches_it(self):
        records = []
        error = AIProviderError("timeout")

        def action(provider, problem, option):
            if problem == "inner":
                self.monitoring.capture_provider_response(raw_response="inner-raw")
                raise error
            self.monitoring.capture_provider_response(returned_model_id="outer-model")
            try:
                provider.extract("inner")
            except AIProviderError:
                self.monitoring.capture_provider_response(raw_response="outer-recovered")
            return "recovered"

        self.assertEqual(self.provider(action=action, observer=records.append).solve("outer"), "recovered")
        self.assertEqual([r.outcome for r in records], ["error", "success"])
        self.assertEqual(records[0].raw_response, "inner-raw")
        self.assertEqual(records[1].raw_response, "outer-recovered")
        self.assertEqual(records[1].returned_model_id, "outer-model")

    def test_parallel_calls_on_one_provider_keep_raw_models_and_outcomes_separate(self):
        barrier = Barrier(2)
        records = []
        error = AIProviderError("rate_limit", status_code=429)

        def action(provider, problem, option):
            self.monitoring.capture_provider_response(
                raw_response=f"private-{problem}", returned_model_id=f"served/{problem}"
            )
            barrier.wait(timeout=5)
            if problem == "failure":
                raise error
            return "success-result"

        provider = self.provider(action=action, observer=records.append)
        with ThreadPoolExecutor(max_workers=2) as executor:
            success = executor.submit(provider.solve, "success")
            failure = executor.submit(provider.extract, "failure")
            self.assertEqual(success.result(timeout=10), "success-result")
            with self.assertRaises(AIProviderError) as caught:
                failure.result(timeout=10)
        self.assertIs(caught.exception, error)
        by_operation = {r.operation: r for r in records}
        self.assertEqual(by_operation["solve"].raw_response, "private-success")
        self.assertEqual(by_operation["solve"].returned_model_id, "served/success")
        self.assertEqual(by_operation["solve"].outcome, "success")
        self.assertEqual(by_operation["extract"].raw_response, "private-failure")
        self.assertEqual(by_operation["extract"].returned_model_id, "served/failure")
        self.assertEqual(by_operation["extract"].outcome, "error")
        self.assertEqual(by_operation["extract"].error_code, "rate_limit")
        self.assertNotEqual(by_operation["solve"].call_id, by_operation["extract"].call_id)


if __name__ == "__main__":
    unittest.main()
