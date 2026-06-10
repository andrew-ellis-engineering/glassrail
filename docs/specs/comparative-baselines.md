# Spec: Comparative baselines (the evidence for the thesis)

Status: Proposed
Priority: P2 — the strongest available launch asset; build during the release
window, publish with the technical deep-dive post
(`docs/release/grassroots-marketing.md` week-2 item).
Depends on: [eval-integrity](eval-integrity.md) (so published numbers include
the held-out suite); [serving-hardening](serving-hardening.md) item 5 helps
but is not required.

## Purpose

Glassrail's pitch is that plan-first DAG execution beats an opaque loop on
inspectability *and* economics: fresh context per node keeps token cost
roughly proportional to declared dependencies, where a ReAct-style loop's
context grows with every step. Nothing currently measures this. The eval
framework already has the subject seam (`evalkit/subjects/`, Decision 9 in
`eval-framework/DECISIONS.md`) and a raw-model backend (`openai-compat`) —
this spec adds a minimal ReAct-loop subject and produces a three-way
comparison on the same tasks: **raw model vs ReAct loop vs glassrail**, on
answer quality (pass@k / pass^k) and tokens per task.

Hard constraints inherited from `eval-framework/CLAUDE.md`: stdlib-only, never
`import glassrail`, subjects reach systems over process/HTTP boundaries, bump
`HARNESS_VERSION` on behavioural changes to running/grading.

## Part 1 — Token accounting in the harness

Read `eval-framework/evalkit/models.py` and the reporter. If trials do not
already carry a token count: add `total_tokens: int | None` to the trial
evidence (populated by each subject where available — the glassrail envelope
exposes `total_tokens`; OpenAI-compatible responses expose `usage`), surface a
mean-tokens column in the suite report table, and persist it in trial
artifacts. This changes runner/reporter behaviour → **bump
`HARNESS_VERSION`** and append the decision to `DECISIONS.md`. (If a token
field already exists, just verify all three subjects populate it.)

## Part 2 — `react-loop` subject

New `eval-framework/evalkit/subjects/react_loop.py`, registered in
`subjects/__init__.py` as `react-loop`. Read
`evalkit/subjects/openai_compat.py` first and reuse its stdlib HTTP pattern
(urllib against `<base_url>/chat/completions`).

Behaviour — a deliberately *standard* tool loop, not a strawman:

- System prompt: answer the user's task; a `file_read` tool is available via
  the OpenAI-native `tools` parameter
  (`{"type": "function", "function": {"name": "file_read", "parameters":
  {path: string}}}`); call tools when needed; give the final answer as plain
  text when done.
- Loop up to `max_turns` (reuse the task's existing `max_turns`, default 8):
  send messages; if the response contains `tool_calls`, execute `file_read`
  **locally in the subject** (plain `Path(path).read_text()` — the eval
  fixtures install under `/tmp/glassrail-eval/`), append the tool-role
  message, continue; otherwise the assistant content is the final answer.
- Accumulate `usage.total_tokens` across turns into the trial's token field.
- Trajectory mapping: one step per tool call,
  `{"tool": "file_read", "input": {...}}` — consistent with the trajectory
  vocabulary so `tool_sequence` criteria grade identically.
- Config mirrors `openai-compat` (`base_url`, `api_key_env`, `extra_body` for
  the Qwen-on-OpenRouter reasoning fix documented in
  `eval-framework/CLAUDE.md`).

Adding a backend is additive — no `HARNESS_VERSION` bump beyond Part 1's.

## Part 3 — Baseline suites

Two new suites, `eval-framework/suites/baseline-react/` and
`suites/baseline-raw/`:

- **Copied** task directories (not symlinks — unlike the OpenRouter mirrors)
  derived from `suites/glassrail/tasks/`, with the glassrail-mechanism
  trajectory criteria **removed** (e.g. `tool_sequence = ["decision"]`,
  `["result"]` — a loop has no decision/result nodes and would fail unfairly).
  Keep all deterministic and LLM answer-quality criteria, and keep the
  `file_read`-usage trajectory criteria for `baseline-react` (the loop has the
  tool). Record in each suite's README which commit of the glassrail suite
  the tasks were copied from, and that they are regenerated — not hand-
  evolved — when the source tasks change.
- `suite.toml`: `default_backend = "react-loop"` / `"openai-compat"`, same
  model (`qwen/qwen3-8b` via OpenRouter), same judge
  (`anthropic/claude-haiku-4.5` via OpenRouter), `k = 3` — identical
  conditions to `glassrail-openrouter` except the subject.

## Part 4 — Run and publish

1. Run all three (`glassrail-openrouter`, `baseline-react`, `baseline-raw`)
   plus `glassrail-heldout` at `--trials 3`, `--workers 5`.
2. Add a **Baselines** section to `docs/evals.md`: the comparison table —
   suite, pass@3, pass^3, mean tokens/task — plus a three-sentence method note
   (same tasks, same model, same judge; what was removed from the copied
   criteria and why; date + harness version). State the result whatever it is
   — if the loop wins somewhere, that is a finding to publish, not hide
   (consistent with the marketing guardrails).
3. Feed the table to the website eval page and the deep-dive post.

## Non-goals

- A multi-framework bake-off (LangGraph etc.) — different stacks, different
  install surfaces; out of scope and off-message
  (`grassroots-marketing.md`: no "killer" comparisons).
- Latency benchmarking (wall time depends on provider load; report tokens).
- Making the baselines part of any release gate — they are evidence, not
  gates.

## Acceptance criteria

- `python3 eval-framework/run.py list` loads both new suites; `--dry-run`
  passes.
- One full three-way run archived under `eval-framework/results/` with
  re-gradable artifacts (`run.py score` works on them).
- `docs/evals.md` Baselines section added (and the page still builds:
  `uv run mkdocs build --strict`).
- `HARNESS_VERSION` bumped once for Part 1; `DECISIONS.md` updated.
