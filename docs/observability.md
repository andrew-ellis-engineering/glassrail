# Observability

`dagagent` is instrumented with [OpenTelemetry](https://opentelemetry.io/)
tracing. A run emits a span tree so you can see, per task, how planning and
execution unfolded and where the time and tokens went.

Tracing is **a no-op until you turn it on**: the planner, router, and executor
only use the OpenTelemetry *API*, which does nothing until an SDK provider is
installed. Off, it costs effectively nothing; on, it exports without any code
change.

## The span tree

```
dagagent.task                      one per run (root)
├── dagagent.plan                  one per planning attempt
│   └── gen_ai.completion          the planner LLM call
└── dagagent.node                  one per executed node
    └── gen_ai.completion          LLM call(s) the node makes
```

Subplan nodes nest the same way: a `dagagent.node` for the subplan contains
the `dagagent.node` spans of its nested plan.

Key attributes (`gen_ai.*` follow the OpenTelemetry GenAI semantic
conventions; `dagagent.*` are ours):

| Span | Attributes |
|------|------------|
| `dagagent.task` | `dagagent.task_id`, `dagagent.task.status` |
| `dagagent.plan` | `dagagent.min_tier`, `dagagent.plan.node_count` |
| `dagagent.node` | `dagagent.node.id`, `dagagent.node.type`, `dagagent.tier`, `dagagent.node.status`, `dagagent.node.confidence` |
| `gen_ai.completion` | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.operation.name`, `gen_ai.usage.total_tokens`, `dagagent.tier` |

A failed node or task sets the span status to `ERROR` with the recorded error.

## Enabling it

The SDK and OTLP exporter ship in the optional `otel` extra:

```bash
uv pip install 'dagagent[otel]'   # or: uv sync --extra otel
```

Then turn tracing on through settings (env vars use the `DAGAGENT_` prefix, or
use `.env` / `config.toml`):

```bash
# Export to an OTLP/HTTP collector (Jaeger, Tempo, the OTel Collector, ...).
export DAGAGENT_OTEL_ENDPOINT=http://localhost:4318/v1/traces
export DAGAGENT_OTEL_SERVICE_NAME=dagagent

uvicorn dagagent.gateways.rest:app
```

| Setting | Env var | Default | Meaning |
|---------|---------|---------|---------|
| `otel_enabled` | `DAGAGENT_OTEL_ENABLED` | `false` | Turn tracing on with no exporter wired (combine with one below). |
| `otel_endpoint` | `DAGAGENT_OTEL_ENDPOINT` | `None` | OTLP/HTTP traces endpoint; setting it enables tracing. |
| `otel_console_export` | `DAGAGENT_OTEL_CONSOLE_EXPORT` | `false` | Also print spans to stdout; enables tracing on its own. |
| `otel_service_name` | `DAGAGENT_OTEL_SERVICE_NAME` | `dagagent` | `service.name` on the resource. |

The gateway calls `configure_tracing(settings)` at startup, so the table above
is all you need to wire it up. To instrument your own entry point, call it
yourself before running the orchestrator:

```python
from dagagent.config import get_settings
from dagagent.telemetry import configure_tracing

configure_tracing(get_settings())
```

## A quick local look

No collector required — print spans to the console:

```bash
DAGAGENT_OTEL_CONSOLE_EXPORT=true uvicorn dagagent.gateways.rest:app
```

Each completed span is written as JSON, so you can eyeball the `task → plan →
node → gen_ai.completion` tree and its attributes while developing.
