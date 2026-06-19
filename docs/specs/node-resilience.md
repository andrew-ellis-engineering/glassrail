# Spec: Node resilience

Status: Part A implemented (2026-06-19); Part B proposed
Priority: P1. Independent of other specs; suggested merge order is after
[parallel-execution](parallel-execution.md) to avoid rebase churn in
`executor.py`.
Depends on: nothing (hard); [serving-hardening](serving-hardening.md) item 1
provides the natural shutdown hook for Part B's `aclose()` — if it has not
landed, wire `aclose()` into the call sites listed below.

## Purpose

The planner has rich failure handling (retries, stall detection, salvage,
feedback threading). The executor has none: when a provider stream dies
mid-generation, `OpenAICompatProvider` raises `ProviderUnavailableError`
*after* the first chunk, the `TierRouter` does not catch it (fallthrough is
pre-first-chunk only, by design), and the node fails permanently with no
retry and no tier escalation. Separately, `OpenAICompatProvider` opens a new
`httpx.AsyncClient` — a fresh TCP+TLS handshake — for **every** completion and
every health check. For a project whose brand is reliability, node-level retry
is the highest-leverage engine change after parallelism, and it is cheap.

## Part A — LLM-node retry with tier escalation

### Settings

New nested model in `src/glassrail/config/settings.py`, exposed as
`settings.resilience` (`[resilience]` in `config.toml`,
`GLASSRAIL_RESILIENCE__*` env):

```python
class ResilienceConfig(BaseModel):
    max_llm_node_retries: int = 1      # extra attempts after the first
    escalate_tier_on_retry: bool = True
```

Document both in the README configuration section (same change — CLAUDE.md
rule).

### Behavioural contract

- **Scope:** the *main* LLM call of `decision`, `synthesis`, `think`,
  `summary`, and `result` nodes (i.e. `_execute_decision` and
  `_execute_llm_node` / `_stream_llm_node` in
  `src/glassrail/executor/executor.py`).
  - **Tool nodes are never auto-retried** — tools may have side effects.
  - The `extract_args` and `shape_check` micro-calls are out of scope for v1
    (they already have soft failure modes).
- **Retryable failures:** (1) `ProviderUnavailableError` escaping the router —
  this covers mid-stream stream death and exhaustion of the eligible tier
  window; (2) an empty collected output (blank text after the stream ends).
- **Not retryable:** `ProviderError` (the 400/422 class — a bad request will
  not succeed on retry), JSON-parse failures (the existing salvage→raw-output
  fallback stands), and any non-provider exception.
- **Retry mechanics:** up to `max_llm_node_retries` extra attempts. When
  `escalate_tier_on_retry` is true, each retry raises the call's `min_tier` to
  `min(previous_attempt_tier + 1, highest_configured_tier)` where
  `previous_attempt_tier` is the tier that served (or was attempting to serve)
  the failed call; when false, the retry repeats with the same tier window.
  Log a warning per retry (structured: node id, attempt, prior tier, error)
  and set an OTel span attribute `glassrail.node.retries` on the node span.
- **Result accounting:** add `retries: int = 0` to `NodeResult`
  (`src/glassrail/core/execution.py`). `tokens_used` accumulates across
  attempts. `tier_used` records the tier of the attempt that produced the
  final outcome.
- **Streaming nuance:** for `_STREAMING_NODE_TYPES`, partial
  `NodeOutputChunk` events may already have been emitted before a mid-stream
  death. v1 policy: the retry simply streams fresh chunks (a transcript may
  show a truncated fragment followed by the full text). No
  retraction/marker event — document this in `docs/streaming.md` in one
  sentence. ACP `isFinal` semantics are unaffected.

### Scripted-provider error directive (test/eval enabler)

Extend `src/glassrail/providers/scripted.py`: when the next JSONL line is an
object of the form `{"__error__": "provider_unavailable"}` the provider raises
`ProviderUnavailableError` instead of yielding; `{"__error__": "provider"}`
raises `ProviderError`. (Raise *before* yielding any chunk so the router's
fallthrough path can also be exercised with scripted tiers.) Unit-test the
directive in `tests/unit/` (note: `src/glassrail/providers/scripted.py`
currently has zero direct tests — add basic happy-path coverage while there).

### New harness-mechanics eval tasks

In `eval-framework/suites/harness-mechanics/tasks/` (regression from day one,
scripted, no model calls — follow the existing task layout: `config.toml`,
`prompt.md` with the `__EXEC_PLAN__` directive, `fixtures/plan.json`,
`fixtures/responses.jsonl`):

- `llm-retry-recovers` — plan: a single `result` node. Responses:
  `{"__error__": "provider_unavailable"}` then a valid
  `{"output": "...", "confidence": 0.9}`. Criteria: node completed, output
  matches.
- `llm-retry-exhausted` — responses: two error lines. With
  `max_llm_node_retries = 1` both attempts fail; criteria: node status
  `failed`, run still completes with no final output (mirror the existing
  `final-output-none` task's criteria shape).

**Fixture caveat (state this in both tasks' config comments):** the exec-plan
subject points all four tiers at the *same* responses file, but the factory
builds one provider per tier, each with its **own** queue — a tier-escalated
retry would pop line 1 of the *next tier's* queue (the error line again).
Therefore both tasks set `GLASSRAIL_RESILIENCE__ESCALATE_TIER_ON_RETRY=false`
in their backend env so the retry stays on tier 0 and pops line 2. Escalation
itself is covered by unit tests with two distinct scripted tiers.

Suite-content additions only — no `HARNESS_VERSION` bump.

### Unit/integration tests

- Retry succeeds: scripted tier raising once then succeeding → node completed,
  `NodeResult.retries == 1`.
- Escalation: tier 0 scripted to raise, tier 1 scripted to succeed,
  `escalate_tier_on_retry=true` → `tier_used == 1`, `retries == 1`.
- Exhaustion: all attempts raise → node `FAILED`, `retries ==
  max_llm_node_retries`, task continues (dependents see the failure notice).
- Tool node with a raising tool → **no** retry (count harness invocations).
- `ProviderError` → no retry.

## Part B — Provider connection reuse and shutdown

### Design

- `OpenAICompatProvider` (`src/glassrail/providers/openai_compat.py`) holds a
  lazily created `httpx.AsyncClient` reused by `complete()` and
  `is_healthy()`; per-request timeouts continue to come from
  `default_timeout_s` via `httpx.Timeout` on the request, not the client.
  Accept an optional `transport: httpx.AsyncBaseTransport | None = None`
  constructor argument so the existing `MockTransport` unit tests inject
  through the persistent client (update
  `tests/unit/test_providers_openai_compat.py` accordingly).
- Add `async def aclose(self) -> None` to the provider; add
  `TierRouter.aclose()` (`src/glassrail/providers/router.py`) that closes every
  wrapped provider exposing `aclose` (duck-typed, same pattern as
  `is_healthy`); add `Runtime.aclose()` (`src/glassrail/runtime.py`) that
  closes the router.
- Call sites for `Runtime.aclose()`: the CLI `run` / `exec-plan` commands
  (`finally` after the task completes), `run_acp` shutdown
  (`src/glassrail/gateways/acp/__init__.py`), and the REST gateway — via the
  lifespan added by [serving-hardening](serving-hardening.md) item 1 if it has
  landed; otherwise leave REST as-is and note it in the PR (the default app
  currently lives for the process lifetime anyway).

## Acceptance criteria

- Full check sweep green.
- `uv run python3 eval-framework/run.py suite eval-framework/suites/harness-mechanics`
  green including the two new tasks.
- Unit tests above green; a test proves the same `httpx.AsyncClient` instance
  serves two consecutive `complete()` calls.
- README documents `[resilience]`; CHANGELOG entry added;
  `docs/streaming.md` notes the retry-restream behaviour.
