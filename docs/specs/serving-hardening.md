# Spec: Serving hardening

Status: Proposed; items 1–2 implemented 2026-06-19, items 5 and 6 implemented 2026-06-11.
Priority: P1, with items 5 (`glassrail run` exit codes) and 6
(`glassrail serve`) suggested early — they are small and user-facing.
Depends on: nothing. Items are independently mergeable; one item per PR.

## Purpose

The serving path carries single-user assumptions that bite the moment anyone
else deploys the Docker image: the runtime is built at *import time*, the
event bus silently drops events under slow consumers, long streams have no
keepalive, `/resume` can race, the CLI exits 0 on failure, and there is no
`serve` command. Each item below fixes one of these.

## Item 1 — Build the runtime in the FastAPI lifespan, not at import — implemented 2026-06-19

**Current:** `src/glassrail/gateways/rest/app.py` ends with
`app = create_default_app()`, which calls `build_runtime(get_settings())`
during module import — settings are frozen before uvicorn even starts, and
there is no shutdown hook.

**Design:**

- `create_default_app()` returns a `FastAPI` app whose **lifespan** context
  manager calls `build_runtime(...)` on startup, stores the `Runtime` on
  `app.state.runtime`, and on shutdown calls `runtime.aclose()` if that method
  exists (added by [node-resilience](node-resilience.md) Part B; guard with
  `hasattr` until then).
- Route handlers stop closing over module-level collaborators and read
  `request.app.state.runtime` (WS handlers: `websocket.app.state.runtime`).
- The test-injection path is preserved: `create_app(orchestrator=..., store=...,
  harness=..., event_bus=...)` pre-populates `app.state` and its lifespan
  skips building when state is already populated. Existing REST integration
  tests must pass with at most mechanical changes.
- `uvicorn glassrail.gateways.rest:app` keeps working (module-level `app`
  remains, but now construction of the runtime is deferred to startup).

## Item 2 — EventBus: drop visibility and per-task subscriptions — implemented 2026-06-19

**Current:** `src/glassrail/events/bus.py` fans out to bounded queues
(`max_queue=1000`) with drop-oldest eviction and **no signal** when an event
is dropped; consumers filter by `task_id` manually.

**Design:**

- Count evictions per subscription; on each eviction emit a rate-limited
  warning log (at most one per subscription per ~10 s) including the running
  drop count, and expose `Subscription.dropped: int` for tests.
- `EventBus.subscribe(task_id: TaskId | None = None)` — when given, the
  subscription enqueues only events whose `task_id` matches (filter at
  publish/enqueue time so unrelated tasks cannot evict this task's events).
- Migrate the REST `_event_source` and the ACP `_run_turn` event loop to
  `subscribe(task_id=...)`; their manual filters become assertions.
- Tests: drop counter increments and warning fires under a full queue; a
  task-scoped subscription never sees another task's events (extend
  `tests/unit/test_events_bus.py`).

## Item 3 — SSE keepalive

**Current:** no traffic during a long-running node; idle proxies and client
timeouts kill the stream silently. (WebSocket is already covered by uvicorn's
protocol-level ping — `--ws-ping-interval` defaults to 20 s; document that in
`docs/streaming.md` rather than reimplementing.)

**Design:** in the SSE wrapper only (`_event_stream` in
`gateways/rest/app.py`), wait on the subscription with a 15 s timeout
(`asyncio.wait_for`); on timeout yield an SSE *comment* frame
`: keepalive\n\n` and continue. Comment frames are invisible to
`data:`-parsing consumers, so the Python TUI client and any JSON consumer are
unaffected — verify `gateways/tui/client.py` skips non-`data:` lines (it
already strips by prefix; add a test). Document the keepalive in
`docs/streaming.md`.

## Item 4 — Resume idempotency

**Current:** `POST /task/{id}/resume` checks status then queues
`orchestrator.resume` as a background task; two near-simultaneous calls can
both pass the check and queue two resumes.

**Design:** in the REST handler, after the status check
(`AWAITING_CONFIRMATION`/`PAUSED`), set `state.status = EXECUTING`, `touch()`,
and `await store.save_task(state)` **before** queueing the background resume.
A second call then fails the status check with the existing 400. Verify
`Orchestrator.resume` tolerates loading a state already marked `EXECUTING`
(it re-drives the executor; adjust its guard if it refuses). Apply the same
check-and-set in the ACP gate path if it shares the race
(`gateways/acp/server.py` `_handle_gate` — verify; it resumes via a created
task after an explicit client response, so the race is narrower there).
Test: two sequential resume calls — second gets 400; task completes once
(count executor invocations with a scripted provider).

## Item 5 — `glassrail run` exit codes (do early) — implemented 2026-06-11

**Current:** `glassrail run` always exits 0, even when the task failed; the
eval harness compensates by reading `is_error` from the JSON envelope.

**Design:**

- After printing the envelope (JSON or plain), raise `typer.Exit(code=1)` when
  the envelope's `is_error` is true / status is `failed`, `rejected`, or
  `cancelled`. Stdout content is unchanged.
- **Required companion (same PR):** read
  `eval-framework/evalkit/subjects/glassrail_cli.py` and make a nonzero exit
  with a *parseable* stdout envelope a normal failed trial — not an
  infra error. A nonzero exit with unparseable stdout remains an error.
  This is a behavioural change to *running* → bump `HARNESS_VERSION` in
  `eval-framework/evalkit/config.py` and append the decision to
  `eval-framework/DECISIONS.md`.
- Apply the same convention to `glassrail exec-plan`.
- Tests: CLI test (see [small-fixes](small-fixes.md) item 9 for the CLI test
  scaffolding — land that first or include the scaffolding here) asserting
  exit 1 on a scripted failure and exit 0 on success.
- README: one sentence under "Headless, one-shot" documenting exit codes.

## Item 6 — `glassrail serve` (do early) — implemented 2026-06-11

**Current:** the README tells users to run
`uv run uvicorn glassrail.gateways.rest:app`; there is no CLI command.

**Design:** a Typer command in `src/glassrail/cli/__init__.py`:

```
glassrail serve [--host 127.0.0.1] [--port 8000] [--reload]
```

implemented as `uvicorn.run("glassrail.gateways.rest:app", host=..., port=...,
reload=...)`. **Default host is `127.0.0.1`** — the secure default; binding
`0.0.0.0` is an explicit operator choice (the Dockerfile already passes it
explicitly, unchanged). uvicorn is already a runtime dependency. Update the
README "Gateway + live viewer" block to lead with `glassrail serve` (keep the
raw uvicorn line as the alternative), and `docs/deployment.md` accordingly.
Test: command exists and `--help` renders (CliRunner); a socket-level smoke is
not required.

## Acceptance criteria

Per item: full check sweep; the named tests; README/docs updated in the same
PR; CHANGELOG entry. Item 5 additionally: harness-mechanics suite green under
the bumped `HARNESS_VERSION`, and one `suites/glassrail-openrouter` task run
end-to-end to prove the subject still classifies pass/fail correctly.
