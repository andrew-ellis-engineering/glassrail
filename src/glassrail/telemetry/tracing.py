"""OpenTelemetry tracing — setup and shared span vocabulary.

The instrumentation scattered through the planner, router, and executor only
ever touches the OpenTelemetry *API*, which is a no-op until an SDK tracer
provider is installed. That keeps tracing free when it's off: spans created
against the default provider don't record and aren't exported.

:func:`configure_tracing` installs an SDK provider when tracing is turned on
(via :class:`~glassrail.config.Settings`), wiring an OTLP exporter and/or a
console exporter. Tests pass their own in-memory exporter. Span names and
attribute keys live here so producers stay consistent — ``gen_ai.*`` keys
follow the OpenTelemetry GenAI semantic conventions; ``glassrail.*`` keys are
ours.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Tracer

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

    from glassrail.config import Settings
    from glassrail.providers.base import LLMProvider

log = logging.getLogger(__name__)

TRACER_NAME = "glassrail"

# Span names.
SPAN_TASK = "glassrail.task"
SPAN_PLAN = "glassrail.plan"
SPAN_NODE = "glassrail.node"
SPAN_LLM = "gen_ai.completion"

# Attribute keys — gen_ai.* follow the GenAI semantic conventions.
ATTR_GEN_AI_SYSTEM = "gen_ai.system"
ATTR_GEN_AI_OPERATION = "gen_ai.operation.name"
ATTR_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
ATTR_GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
ATTR_TASK_ID = "glassrail.task_id"
ATTR_TASK_STATUS = "glassrail.task.status"
ATTR_PLAN_REJECTION_REASON = "glassrail.plan.rejection_reason"
ATTR_PLAN_NODE_COUNT = "glassrail.plan.node_count"
ATTR_NODE_ID = "glassrail.node.id"
ATTR_NODE_TYPE = "glassrail.node.type"
ATTR_NODE_STATUS = "glassrail.node.status"
ATTR_NODE_CONFIDENCE = "glassrail.node.confidence"
ATTR_TIER = "glassrail.tier"
ATTR_MIN_TIER = "glassrail.min_tier"

LLM_SPAN_KIND = SpanKind.CLIENT


def get_tracer() -> Tracer:
    """Return the glassrail tracer (no-op until :func:`configure_tracing` runs)."""
    return trace.get_tracer(TRACER_NAME)


def _has_sdk_provider() -> bool:
    """True if a real (non-proxy) tracer provider is already installed.

    The default API provider is a proxy without ``add_span_processor``; any SDK
    ``TracerProvider`` has it. Checking this keeps :func:`configure_tracing`
    idempotent without a module-level flag or fighting the API's set-once guard.
    """
    return hasattr(trace.get_tracer_provider(), "add_span_processor")


def provider_model(provider: LLMProvider) -> str | None:
    """Best-effort model name for a provider, for ``gen_ai.request.model``.

    The :class:`~glassrail.providers.base.LLMProvider` protocol doesn't require
    a ``model``, so this stays optional rather than widening the protocol.
    """
    model = getattr(provider, "model", None)
    return model if isinstance(model, str) else None


def configure_tracing(settings: Settings, *, span_exporter: SpanExporter | None = None) -> bool:
    """Install an SDK tracer provider when tracing is enabled.

    Enabled when ``settings.otel_enabled`` is set, an OTLP endpoint is
    configured, or a ``span_exporter`` is passed (tests). Returns whether a
    provider was installed; a no-op (returning ``False``) when off or when the
    SDK isn't installed. Safe to call more than once — only the first call
    installs a provider.
    """
    enabled = (
        settings.otel_enabled
        or settings.otel_endpoint is not None
        or settings.otel_console_export
        or span_exporter is not None
    )
    if not enabled:
        return False
    if _has_sdk_provider():
        return True

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
    except ImportError:
        log.warning("OpenTelemetry SDK not installed; install the 'otel' extra to export traces")
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )

    if span_exporter is not None:
        # In-process exporter (tests): flush synchronously so spans are visible
        # the moment the run ends.
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        if settings.otel_endpoint is not None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
            )
        if settings.otel_console_export:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    log.info("OpenTelemetry tracing enabled (service=%s)", settings.otel_service_name)
    return True
