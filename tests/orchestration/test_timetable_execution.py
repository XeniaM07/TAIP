"""Bounded independent provider stages with private per-call observations."""

from collections import OrderedDict
import threading
import unittest

from taip.ai_integration.errors import AIProviderError
from taip.common.ai_execution import classify_ai_failure


class ExecutionTests(unittest.TestCase):
    def executor(self, **options):
        from taip.orchestration.timetable_execution import TimetableExecutionCoordinator
        result = TimetableExecutionCoordinator(**options)
        self.addCleanup(result.close)
        return result

    def test_independent_calls_overlap_and_return_in_configured_order(self):
        executor = self.executor(max_workers=3, max_pending=3)
        barrier = threading.Barrier(3)
        def call(value):
            barrier.wait(timeout=2)
            return value
        outcomes = executor.execute("extract", OrderedDict((name, lambda n=name: call(n)) for name in ("c", "a", "b")))
        self.assertEqual(list(outcomes), ["c", "a", "b"])
        self.assertEqual([v.result for v in outcomes.values()], ["c", "a", "b"])
        self.assertTrue(all(v.failure is None for v in outcomes.values()))

    def test_busy_batch_does_not_queue_or_call_any_provider(self):
        executor = self.executor(max_workers=1, max_pending=1)
        entered, release = threading.Event(), threading.Event()
        def blocking():
            entered.set()
            if not release.wait(3): raise RuntimeError("test did not release worker")
            return "complete"
        result = []
        thread = threading.Thread(target=lambda: result.append(executor.execute("extract", {"a": blocking})))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            calls = []
            denied = executor.execute("extract", {"b": lambda: calls.append(1)})
            self.assertEqual(denied["b"].failure.code, "coordinator_busy")
            self.assertEqual(calls, [])
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(result[0]["a"].result, "complete")
        self.assertEqual(executor.execute("solve", {"c": lambda: "reused"})["c"].result, "reused")

    def test_one_failure_isolated_and_private_text_is_not_public_metadata(self):
        executor = self.executor()
        failure = AIProviderError("rate_limit", retry_after_seconds=2)
        def fail(): raise failure
        outcomes = executor.execute("extract", {"a": fail, "b": lambda: "ok"})
        self.assertEqual(outcomes["a"].failure.code, "rate_limit")
        self.assertEqual(outcomes["a"].failure.retry_after_seconds, 2)
        self.assertIs(outcomes["a"].exception, failure)
        self.assertEqual(outcomes["b"].result, "ok")
        safe = classify_ai_failure(RuntimeError("secret-value-not-for-logs"))
        self.assertNotIn("secret-value", repr(safe))

    def test_inline_execution_preserves_order_and_captures_aop_without_global_history(self):
        from taip.orchestration.timetable_execution import InlineAIExecution
        from taip.ai_integration.mock_provider import MockAIProvider
        from taip.common.timetable_contracts import OriginalProblem
        from tests.ai_integration.test_mock_provider import EXTRACTION_RESPONSE, PROMPT
        provider = MockAIProvider("a", extraction_responses={PROMPT: EXTRACTION_RESPONSE}, solution_responses={})
        result = InlineAIExecution().execute("extract", {"a": lambda: provider.extract(OriginalProblem("p", PROMPT))})["a"]
        self.assertIsNone(result.failure)
        self.assertEqual(result.invocation["model_id"], "a")
        self.assertEqual(result.raw_response, EXTRACTION_RESPONSE)
        self.assertNotIn("raw_response", result.invocation)
        self.assertNotIn("result", result.invocation)

    def test_closed_coordinator_does_not_invoke_provider(self):
        executor = self.executor()
        executor.close()
        calls = []
        result = executor.execute("extract", {"a": lambda: calls.append(1)})
        self.assertEqual(result["a"].failure.code, "coordinator_busy")
        self.assertEqual(calls, [])

    def test_pre_dispatch_quota_rejection_does_not_consume_a_paid_attempt_slot(self):
        executor = self.executor()
        def rejected(): raise AIProviderError("local_rate_limit", retry_after_seconds=5)
        result = executor.execute("extract", {"a": rejected})["a"]
        self.assertEqual(result.failure.code, "local_rate_limit")
        self.assertFalse(result.attempted)

    def test_provider_refusals_truncation_and_transport_categories_stay_explicit(self):
        for code in ("provider_refusal", "output_truncated", "unsupported_encoding", "provider_error", "http_error"):
            with self.subTest(code=code):
                failure = classify_ai_failure(AIProviderError(code))
                self.assertEqual(failure.code, code)
                self.assertFalse(failure.retryable)
