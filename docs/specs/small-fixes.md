# Spec: Small fixes and API cleanup

Status: Proposed
Priority: mixed — **item 1 and item 9 land before the 0.1.0 tag** (public API
name; eval-contract coverage). Everything else is independent and any-time.
One item (or a small coherent batch) per PR. For each: full check sweep,
CHANGELOG entry, README when a surface changes.

## 1. Rename `DagagentError` → `GlassrailError` (pre-tag) — implemented 2026-06-10

`src/glassrail/core/errors.py` roots the exception hierarchy at
`DagagentError` — the project's pre-rename brand, about to ship as public API.
Rename the class; update every reference (`grep -rn "DagagentError" src/
tests/` — includes `providers/base.py`'s `ProviderError` parentage). No
deprecation alias: there are zero external users before the first release.
The eval framework never imports glassrail, so it is unaffected.

## 2. Fold the stray prompts into `NodePrompts`

Every node role's system prompt is configurable via `settings.prompts` —
except three:

- the **extract-args** prompt is hardcoded inline in
  `src/glassrail/executor/executor.py` (in `_extract_args`), despite
  `budgets.extract_args` existing;
- the **concise** and **verbose** summary prompts are read from module
  constants (`SUMMARY_CONCISE_SYSTEM` / `SUMMARY_VERBOSE_SYSTEM` in
  `config/prompts.py`) in `_node_system_prompt`, bypassing `settings.prompts`
  (only the medium summary prompt is configurable).

Add `extract_args`, `summary_concise`, and `summary_verbose` fields to the
`NodePrompts` settings model with defaults equal to the current strings
(move the inline extract-args text to `config/prompts.py` as
`DEFAULT_EXTRACT_ARGS_SYSTEM`); make the executor read all three from
`settings.prompts`. Tests: extend `tests/unit/test_config_prompts.py` and the
executor prompt-dispatch tests (custom prompt for each of the three is
honoured). README "Node prompts" section: note all roles are now overridable.

## 3. Remove the dead validator check

`PlanValidator._check_branch_references` in
`src/glassrail/validator/validator.py` re-validates branch-target existence
that `topo_sort` (which runs earlier in `validate()`) already raises on, with
the same message — the second check is unreachable for that case. Write a
test proving `topo_sort` raises on a missing branch target (if one doesn't
already exist), then delete `_check_branch_references` and its call site.

## 4. Move `ToolRisk` to `core` (fixes a layer inversion)

`src/glassrail/events/types.py` imports `ToolRisk` from
`glassrail.harness` — the events package importing *upward* into harness,
the only inversion of the "everything imports core, core imports nothing"
rule besides item 4's twin in `planner/tool_digest.py`. Move the `ToolRisk`
literal type into `core` (e.g. `core/plan.py` alongside the node vocabulary,
re-exported from `core/__init__.py`); `harness/registry.py` and
`events/types.py` import it from core; keep a re-export in
`glassrail.harness` so existing imports keep working. While there:
`planner/tool_digest.py` imports `ToolSchema` from `harness.registry` —
re-export `ToolSchema` from `glassrail.harness.__init__` and import from the
package, removing the submodule reach-in.

## 5. Consolidate the `_Scripted` test fake

The scripted-provider fake is copy-pasted into six test files (grep
`class _Scripted` under `tests/`). Add factories to `tests/conftest.py` —
`make_scripted(responses: list[str])` and a capturing variant exposing
`system_seen` / `user_seen` / `max_tokens_seen` — matching the strictest
existing copy (raises `RuntimeError("scripted exhausted")` on over-call).
Migrate all six files; behaviour-identical, assertion changes only where
attribute names differ.

## 6. Delete the unused `Planner.plan()`

`Planner.plan()` in `src/glassrail/planner/planner.py` implements a fixed
two-attempt strategy that **production never calls** — the orchestrator drives
`plan_attempt()` directly with its own retry loop. Two public entry points
with different retry semantics is a trap. Delete `plan()`, migrate its unit
tests to `plan_attempt()`/orchestrator-level equivalents, and delete the
legacy `PLANNER_SYSTEM` re-export alias in `planner/__init__.py`. CHANGELOG
under `[Unreleased]` (pre-1.0 breaking change, called out plainly).

## 7. Subplan child task-id isolation

`_execute_subplan` in `executor.py` builds the child `ExecutionState` with
the **parent's** `task_id` (a code comment acknowledges the collision risk;
it is safe only because child state is never persisted). Derive a distinct,
traceable id: `TaskId(f"{state.task_id}-sub{node.id}")`. Verify nothing keys
on the child id equalling the parent's (events from the child are currently
suppressed; after [parallel-execution](parallel-execution.md) Part B they
carry `node_path`, and a distinct task-id prefix keeps any future persistence
collision-free). Test: child state's task_id is distinct and prefixed.

## 8. Subplan confidence from the inner plan

`_execute_subplan` hardcodes `confidence=1.0` on success, masking low-quality
nested output from the flag check. Set the subplan node's confidence to the
confidence of the inner node whose output became the nested `final_output`
(the same result→synthesis→summary selection the executor already performs;
expose the chosen node's result rather than re-deriving), defaulting to 1.0
only when that is unavailable. The flag check then applies naturally. Tests:
inner result confidence 0.4 → subplan node flagged; missing → 1.0.

## 9. CLI tests (pre-tag) — implemented 2026-06-10

`src/glassrail/cli/__init__.py` has **zero** tests, and `glassrail run
--json` is the contract the entire eval framework consumes — it can drift
silently. Add `tests/unit/test_cli.py` using `typer.testing.CliRunner`:

- `version` prints the package version.
- `run "<task>" --json` end-to-end over **scripted tiers**: build a tmp
  JSONL of canned responses, point all four tiers at it via
  `GLASSRAIL_TIER{0..3}__KIND=scripted` + `__SCRIPTED_PATH` env (the
  `TierConfig` `scripted` kind exists for exactly this), and assert the
  envelope contains every documented key (`result`, `trajectory`, `status`,
  `is_error`, `error`, `total_tokens`, `task_id`, `replan_count`, `plan`,
  `planning_attempts`, `branch_log`, `flagged_nodes`) with correct types —
  this is the envelope **golden test**.
- `exec-plan <fixture> --json` happy path reusing a harness-mechanics fixture
  plan + responses file.
- `tui --help` / `acp --help` render (no execution).

## 10. Tests for `strip_model_output`

`src/glassrail/providers/postprocess.py` cleans every raw LLM string before
JSON parsing and has zero direct tests — a regression here fails every node
with "invalid JSON" and no obvious cause. Add `tests/unit/test_providers_postprocess.py`:
`<think>…</think>` block stripped (including multiline + leading whitespace),
single ```json fence unwrapped, fenceless passthrough, text containing a
fence mid-string left alone, empty string.

## 11. Document the image tool — implemented 2026-06-10

`image_generate` (`harness/integrations/image.py` — mflux/Flux text-to-image
and img2img, `[tools.image]`, risk `write`, macOS + `mflux` binary required)
is fully implemented and absent from the README. Add a short block under the
README Tools section mirroring the web-integration block: what it does, the
extra/binary requirement, `[tools.image] enabled = true`, off by default.
(Doc-only; pairs with the `docs/release/pre-release-hygiene.md` checklist.)

## Acceptance criteria

Per item: check sweep green; the named tests; grep-clean where the item is a
removal (`! grep -rn "DagagentError" src/ tests/` for item 1; no
`class _Scripted` outside conftest for item 5; no `def plan(` on Planner for
item 6). Items touching executor/planner/validator also run the
harness-mechanics suite.
