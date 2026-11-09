"""Explicit reusable application composition; importing never loads credentials."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
import os
from threading import Lock
from typing import Any

from taip.ai_integration.config import ProviderConfig, load_provider_configs
from taip.ai_integration.errors import AIConfigurationError
from taip.ai_integration.factory import AIProviderFactory
from taip.ai_integration.http_transport import HTTPJSONTransport, JSONTransport
from taip.ai_integration.monitoring import InvocationRecord
from taip.ai_integration.rate_limits import RateLimitPort
from taip.application.workflow import (
    MOCK_DEMO_PROMPT, InMemorySessionRepository, MockProviderFactory, WorkflowService,
)
from taip.common.ai_execution import AIExecutionPort
from taip.mathematical_verification.timetable_verifier import TimetableVerifier
from taip.orchestration.timetable_execution import TimetableExecutionCoordinator


def _quota_warnings(model: dict[str, Any]) -> list[str]:
    limits, quota = model["limits"], model["quota"]
    if limits is None or quota is None:
        return []
    warnings = []
    if quota["cooldown_seconds"] > 0:
        warnings.append("Provider cooldown is active; wait before retrying.")
    for used, limit, label in (
        ("requests_in_minute", "requests_per_minute", "minute request budget"),
        ("requests_in_day", "requests_per_day", "daily request budget"),
        ("tokens_in_minute", "tokens_per_minute", "minute token budget"),
        ("in_flight", "max_concurrent", "concurrent request budget"),
    ):
        if quota[used] >= limits[limit] * limits["warning_fraction"]:
            warnings.append(f"Configured {label} is near its limit.")
    return warnings


def runtime_info_for_service(workflow: WorkflowService) -> dict[str, Any]:
    """Read safe catalog metadata and quota counters without checking vendors."""
    factory = workflow.providers
    if isinstance(factory, AIProviderFactory):
        mode = "live"
        models = list(factory.model_catalog())
        for model in models:
            try:
                model["quota"] = asdict(factory.quota_status(model["model_id"]))
            except Exception:
                # A Redis outage must not leak its connection details or hide
                # other models' availability in this read-only metadata route.
                model["quota"] = None
                model["warnings"] = ["Quota information is temporarily unavailable."]
            else:
                model["warnings"] = _quota_warnings(model)
    else:
        mode = "mock" if isinstance(factory, MockProviderFactory) else "custom"
        model_ids = tuple(getattr(factory, "MODEL_IDS", MockProviderFactory.MODEL_IDS))
        models = [{
            "model_id": model_id, "label": model_id, "provider": mode,
            "requested_model_id": model_id, "limits": None, "quota": None,
            "warnings": [],
        } for model_id in model_ids]
    return {
        "mode": mode, "models": models,
        "demo_prompt": MOCK_DEMO_PROMPT if mode == "mock" else None,
    }


class RuntimeResources:
    """One workflow composition and only the resources its builder created."""

    def __init__(
        self, workflow: WorkflowService, execution: AIExecutionPort,
        transport: JSONTransport | None, invocations: deque[InvocationRecord],
        owned_resources: Iterable[Any],
    ) -> None:
        self.workflow = workflow
        self.execution = execution
        self.transport = transport
        # Private observations are bounded and never returned by runtime_info.
        self.invocations: deque[InvocationRecord] = invocations
        self._owned_resources = tuple(owned_resources)
        self._close_lock = Lock()
        self._closed = False

    def runtime_info(self) -> dict[str, Any]:
        return runtime_info_for_service(self.workflow)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            for resource in reversed(self._owned_resources):
                try:
                    resource.close()
                except Exception:
                    # Continue closing the independent connection pool if the
                    # coordinator's shutdown itself fails.
                    pass
        finally:
            self.invocations.clear()


def build_runtime(
    configs: Iterable[ProviderConfig] | None = None, *,
    transport: JSONTransport | None = None,
    limiter: RateLimitPort | None = None,
    execution: AIExecutionPort | None = None,
) -> RuntimeResources:
    """Compose mock or configured providers with bounded parallel execution."""
    owned_resources = []
    invocations: deque[InvocationRecord] = deque(maxlen=100)
    try:
        if configs is None:
            factory = MockProviderFactory()
        else:
            if transport is None:
                transport = HTTPJSONTransport()
                owned_resources.append(transport)
            factory = AIProviderFactory(
                configs, transport=transport, observer=invocations.append, limiter=limiter,
            )
        if execution is None:
            execution = TimetableExecutionCoordinator(max_workers=6, max_pending=6)
            owned_resources.append(execution)
        workflow = WorkflowService(
            InMemorySessionRepository(), factory, TimetableVerifier(), execution=execution,
        )
        if configs is None:
            workflow.mock_demo_prompt = MOCK_DEMO_PROMPT
        return RuntimeResources(workflow, execution, transport, invocations, owned_resources)
    except BaseException:
        for resource in reversed(owned_resources):
            try:
                resource.close()
            except Exception:
                pass
        raise


def create_runtime_app(environment: Mapping[str, str] | None = None):
    """Local ASGI factory; live mode is explicit and configuration fails closed."""
    from taip.application.api import create_app

    source = os.environ if environment is None else environment
    enabled = source.get("TAIP_LIVE_AI", "false")
    if enabled not in ("true", "false"):
        raise AIConfigurationError("TAIP_LIVE_AI")
    configs = load_provider_configs(source) if enabled == "true" else None
    runtime = build_runtime(configs)
    try:
        app = create_app(runtime.workflow, runtime_info=runtime.runtime_info)

        @asynccontextmanager
        async def lifespan(app):
            try:
                yield
            finally:
                runtime.close()

        app.router.lifespan_context = lifespan
        app.state.runtime = runtime
        return app
    except BaseException:
        runtime.close()
        raise
