"""Bounded provider execution; session and benchmark writes stay with the caller."""

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import asdict
from threading import Lock
from time import perf_counter
from typing import Literal

from taip.ai_integration.monitoring import InvocationRecord, observe_invocations
from taip.common.ai_execution import AICallFailure, AICallOutcome, classify_ai_failure


def _summary(record: InvocationRecord) -> dict[str, object]:
    return {"call_id": record.call_id, "operation": record.operation, "provider": record.provider,
            "model_id": record.model_id, "requested_model_id": record.requested_model_id,
            "returned_model_id": record.returned_model_id, "upstream_provider": record.upstream_provider,
            "duration_ms": record.duration_ms, "outcome": record.outcome, "error_code": record.error_code,
            "http_status_code": record.http_status_code, "retry_after_seconds": record.retry_after_seconds,
            "generation_parameters": asdict(record.generation_parameters) if record.generation_parameters else None,
            "usage": asdict(record.usage) if record.usage else None}


def _call(operation: str, call: Callable[[], object]) -> AICallOutcome:
    records: list[InvocationRecord] = []
    result, error = None, None
    started = perf_counter()
    with observe_invocations(records.append):
        try:
            result = call()
        except Exception as caught:
            error = caught
    duration = round((perf_counter() - started) * 1000, 3)
    record = next((item for item in reversed(records) if item.operation == operation), None)
    raw = record.raw_response if record else getattr(result if error is None else error, "raw_response", None)
    failure = classify_ai_failure(error) if error else None
    # These categories are emitted only before transport dispatch. Ambiguous
    # network/settlement errors remain attempts; they must never be auto-retried.
    attempted = failure is None or failure.code not in {"local_rate_limit", "request_budget_exceeded", "configuration_error"}
    return AICallOutcome(result=result, failure=failure,
                         duration_ms=duration, invocation=_summary(record) if record else None,
                         raw_response=raw if isinstance(raw, str) else None, exception=error, attempted=attempted)


class InlineAIExecution:
    """Synchronous compatibility composition using the same outcome contracts."""

    def execute(self, operation: Literal["extract", "solve"], calls: Mapping[str, Callable[[], object]]) -> dict[str, AICallOutcome]:
        return {name: _call(operation, call) for name, call in calls.items()}


class TimetableExecutionCoordinator:
    """Admit a whole batch immediately or reject it without queueing provider work.

    A fixed worker pool is reused for the runtime lifetime. Reserved work never
    exceeds the worker count, so waiting HTTP requests cannot build an unbounded
    executor queue. Only independent provider calls run on these workers.
    """

    def __init__(self, *, max_workers: int = 6, max_pending: int = 6) -> None:
        if (type(max_workers) is not int or type(max_pending) is not int
                or not 1 <= max_pending <= max_workers <= 64):
            raise ValueError("Invalid AI execution capacity.")
        self._capacity = max_pending
        self._reserved = 0
        self._closed = False
        self._lock = Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="taip-ai")

    def execute(self, operation: Literal["extract", "solve"], calls: Mapping[str, Callable[[], object]]) -> dict[str, AICallOutcome]:
        entries = tuple(calls.items())
        if not entries:
            return {}
        with self._lock:
            if self._closed or len(entries) > self._capacity - self._reserved:
                failure = AICallFailure("coordinator_busy", "AI execution is busy. Try again after the active operation finishes.", True, 1)
                return {name: AICallOutcome(failure=failure, attempted=False) for name, _ in entries}
            self._reserved += len(entries)
            futures = {}
            try:
                for name, call in entries:
                    futures[name] = self._pool.submit(copy_context().run, _call, operation, call)
            except Exception:
                # Partial startup failures still retain any completed paid work.
                submission_failure = True
            else:
                submission_failure = False
        try:
            outcomes = {name: future.result() for name, future in futures.items()}
            if submission_failure:
                failure = AICallFailure("coordinator_busy", "AI execution could not be started.", True, 1)
                outcomes.update((name, AICallOutcome(failure=failure, attempted=False)) for name, _ in entries if name not in outcomes)
            return outcomes
        finally:
            # Releasing admission early could queue duplicate work after caller
            # interruption while an already started provider continues running.
            wait(futures.values())
            with self._lock:
                self._reserved -= len(entries)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=False)
