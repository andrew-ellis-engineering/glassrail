# Engineering specs

Implementable specifications produced by the June 2026 full-repo architecture
audit. Each spec is self-contained: it states the current behaviour, the
required behaviour, the design decision (one design — no open choices), the
files to change, the tests to add, and runnable acceptance criteria. They are
written to be implemented by a coding agent (Claude Sonnet/Opus, GPT-class)
without access to the audit conversation.

The roadmap ([roadmap](../roadmap.md)) owns *when* these happen; the specs own
*how*. Release-process specs live separately in `docs/release/`.

## Status and sequencing

| Spec | Priority | Depends on | Status |
|---|---|---|---|
| [Eval integrity](eval-integrity.md) | **P0 — blocks the 0.1.0 tag** | — | Implemented for the 0.1.0 gate; promotion ratchet ongoing |
| [Security baseline](security-baseline.md) | P0 — items 1, 2, 5 before the tag; 3–4 before broad marketing | — | Implemented |
| [Small fixes](small-fixes.md) | Mixed — item 1 (error rename) and item 9 (CLI tests) before the tag; rest anytime | — | Implemented |
| [Serving hardening](serving-hardening.md) | P1 — item 5 (exit codes) and item 6 (`glassrail serve`) early | — | Implemented |
| [Parallel execution](parallel-execution.md) | P1 — first engine workstream; prerequisite for `foreach` | — | Implemented |
| [Node resilience](node-resilience.md) | P1 — independent; suggested after parallel execution to avoid rebase churn | — | Implemented |
| [Routing table](routing-table.md) | P1 — prerequisite for the Phase 2.5 tier-ROI selector | — | Implemented |
| [Comparative baselines](comparative-baselines.md) | P2 — launch evidence asset | Eval integrity (held-out suite) | Harness/suites implemented; full runs pending |

Suggested engine order: parallel-execution → node-resilience → routing-table →
serving-hardening (remaining items) → `foreach` (external vault spec). The P0
specs and small fixes can proceed in parallel with anything.

## How to implement a spec (instructions for the implementing agent)

1. **Read `CLAUDE.md` (or `AGENTS.md` — same content) in full first.** The
   conventions there are locked: pyright strict with zero warnings, stdlib
   `asyncio` only, ruff, `uv`, src-layout, `core/` imports nothing.
2. **One spec — or one numbered item from a multi-item spec — per branch/PR.**
   Branch off `main`. Do not bundle unrelated items.
3. **Verify before editing.** These specs were written 2026-06-10 against
   commit `c79cebb` plus the in-flight release working tree. File paths and
   symbol names are accurate as of then; line numbers are hints only. `grep -n`
   for every named symbol before changing it. If a named behaviour has already
   changed, the spec's *behavioural contract* sections are normative — adapt
   the mechanics, keep the contract.
4. **Check sweep before claiming done** (zero failures, zero lint findings,
   pyright clean with zero warnings):

   ```bash
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   uv run pyright
   ```

   When the change touches docs or `mkdocs.yml`: `uv run mkdocs build --strict`.
   When the change touches the executor, providers, planner, or validator, also
   run the deterministic eval regression wall (no model calls, ~10 s):

   ```bash
   uv run python3 eval-framework/run.py suite eval-framework/suites/harness-mechanics
   ```

5. **Side obligations** (from `CLAUDE.md`): update `README.md` in the same
   change when a CLI command, flag, default, or config key changes; add a
   `CHANGELOG.md` entry under `[Unreleased]`; new docs pages go into the
   `mkdocs.yml` `nav`.
6. **Eval framework rules** (from `eval-framework/CLAUDE.md`): it is stdlib-only
   — never add a dependency there, never `import glassrail` from it; bump
   `HARNESS_VERSION` in `evalkit/config.py` on any behavioural change to
   *running or grading* (adding suites/tasks is not a bump; changing a subject's
   classification of results is).
7. **The Goodhart rule is absolute**: never encode vocabulary from eval task
   prompts into engine code, cookbook keywords, or default prompts, and never
   iterate prompts or heuristics against the held-out suite. See
   [eval-integrity](eval-integrity.md).
8. When finished, flip the spec's `Status:` line to
   `Implemented (YYYY-MM-DD)` and update the table above in the same PR.
