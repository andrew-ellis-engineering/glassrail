"""Observability — OpenTelemetry tracing for the planner, router, and executor.

A run emits a span tree (task -> plan / node -> LLM call). It's a no-op until
:func:`configure_tracing` installs an SDK provider, so importing and creating
spans is free when tracing is off.
"""

from __future__ import annotations

from glassrail.telemetry.tracing import (
    ATTR_GEN_AI_OPERATION,
    ATTR_GEN_AI_REQUEST_MODEL,
    ATTR_GEN_AI_SYSTEM,
    ATTR_GEN_AI_USAGE_TOTAL_TOKENS,
    ATTR_MIN_TIER,
    ATTR_NODE_CONFIDENCE,
    ATTR_NODE_ID,
    ATTR_NODE_RETRIES,
    ATTR_NODE_STATUS,
    ATTR_NODE_TYPE,
    ATTR_PLAN_NODE_COUNT,
    ATTR_PLAN_REJECTION_REASON,
    ATTR_TASK_ID,
    ATTR_TASK_STATUS,
    ATTR_TIER,
    LLM_SPAN_KIND,
    SPAN_LLM,
    SPAN_NODE,
    SPAN_PLAN,
    SPAN_TASK,
    configure_tracing,
    get_tracer,
    provider_model,
)

__all__ = [
    "ATTR_GEN_AI_OPERATION",
    "ATTR_GEN_AI_REQUEST_MODEL",
    "ATTR_GEN_AI_SYSTEM",
    "ATTR_GEN_AI_USAGE_TOTAL_TOKENS",
    "ATTR_MIN_TIER",
    "ATTR_NODE_CONFIDENCE",
    "ATTR_NODE_ID",
    "ATTR_NODE_RETRIES",
    "ATTR_NODE_STATUS",
    "ATTR_NODE_TYPE",
    "ATTR_PLAN_NODE_COUNT",
    "ATTR_PLAN_REJECTION_REASON",
    "ATTR_TASK_ID",
    "ATTR_TASK_STATUS",
    "ATTR_TIER",
    "LLM_SPAN_KIND",
    "SPAN_LLM",
    "SPAN_NODE",
    "SPAN_PLAN",
    "SPAN_TASK",
    "configure_tracing",
    "get_tracer",
    "provider_model",
]
