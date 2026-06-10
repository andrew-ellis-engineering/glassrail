# Spec: Eval integrity

Status: Proposed
Priority: P0 — blocks the 0.1.0 tag and all marketing claims about eval results.
Depends on: nothing.

## Purpose

Make the published eval numbers mean what the marketing says they mean. The
June 2026 audit found three integrity problems:

1. **Eval-task vocabulary is hardcoded into engine code.**
   `_looks_like_conditional_request` in `src/glassrail/executor/orchestrator.py`
   enumerates `" even or odd "`, `" odd or even "`, `" northern or southern "`,
   `" southern or northern "`, `" true or false "`, `" yes or no "` — the
   literal phrasings of the `classify-even`/`classify-odd` and
   `hemisphere-north`/`hemisphere-south` eval tasks. The planner cookbook
   `src/glassrail/planner/cookbooks/conditional_branch.json` ranks on keywords
   `even, odd, northern, southern, hemisphere`; `compare_aggregate.json` ranks
   on domain nouns from the recommend/research tasks (`throughput, durability,
   latency, license, sql`); `direct_answer.json` contains phrasings from
   `reason-logic` (`who owns`, `exactly one`, `does not own`). The gate score
   therefore partially measures string-memorisation of the suite (Goodhart).
2. **No held-out measurement exists.** Every eval task has been visible during
   prompt/heuristic iteration, so there is no number that estimates
   generalisation.
3. **The release gate is not mechanically enforced.** The CI `eval-framework`
   job only compiles and dry-runs; the `harness-mechanics` suite (32 regression
   tasks, scripted provider, **zero model calls**, ~10 s) is never actually run
   in CI. Additionally, `eval-framework/suites/glassrail/EVAL_PLAN.md` defines
   the Phase 1 gate as a *promoted regression set at pass^5 = 1.0*, but zero of
   the 23 glassrail-suite tasks have ever been promoted via the ratchet; the
   gate declared met in `docs/roadmap.md` used a different (capability
   full-pass-percentage) definition on the OpenRouter mirror suites. The
   roadmap now owns the single operative gate definition (see its
   "Gate definition and integrity caveats" section); this spec makes the
   mechanical parts of it real.

Background absorbed from the deleted `PHASE1_REMAINING.md` (historical,
local-MLX-era promotion blockers, kept for context): `subplan-correct` once
rarely emitted subplans (fixed by prompt work; 3/3 in `run-20260608T185414Z`);
`hemisphere-north` failed every trial on the local model (passes on OpenRouter);
`classify-odd` / `decision-correct-no` / `think-intermediate-correct` had
node-shape criteria tension (since resolved by criteria separation).

## Non-goals

- Weakening any existing criterion to improve numbers (explicitly banned by
  `docs/release/pre-release-hygiene.md`).
- Removing the conditional-structure retry entirely — it earns its keep; it
  just must be structural.
- Live-web eval tasks (still excluded by the temporal firewall; see
  EVAL_PLAN.md).

## Part 1 — Structural conditional detection

**File:** `src/glassrail/executor/orchestrator.py`, function
`_looks_like_conditional_request` (module-level, near `_structural_retry_feedback`).

**Behavioural contract:** the function returns `True` when a request's *shape*
demands a runtime branch, using only structural signals — generic connectives
and grammatical patterns. It must contain no domain nouns and no phrases
copied from eval prompts.

Replace the `conditional_markers` tuple as follows:

- **Keep** the generic connectives: `" otherwise "`, `" else "`, `" whether "`.
- **Delete** all six enumerated binary pairs (`" even or odd "`,
  `" odd or even "`, `" northern or southern "`, `" southern or northern "`,
  `" true or false "`, `" yes or no "`).
- **Add** one structural pattern, a module-level compiled regex that matches an
  interrogative-verb-led binary-alternative question in a single sentence:

  ```python
  _BINARY_QUESTION_RE = re.compile(
      r"\b(?:is|are|was|were|does|do|did|will|would|should|has|have|can|could)\b"
      r"[^.?!]{0,80}?\s+or\s+[^.?!]{1,40}?\?",
      re.IGNORECASE,
  )
  ```

  This matches "Is 246 even or odd?", "Is Sydney … in the northern or southern
  hemisphere? …", "Is 97 prime or composite?", "Was 1900 a leap year or not?"
  — without naming any domain.

- **Suppression guard:** the binary-question pattern (not the connectives) is
  suppressed when the request also contains a comparison/recommendation marker
  (`"recommend"`, `"compare"`, `" vs "`, `" vs."`, `"versus"`, `"trade-off"`,
  `"tradeoff"`), because those requests route to compare/synthesise plan shapes
  where forcing a decision node is wrong.
- Keep the existing `if … then` / branch-verb machinery and the
  `optional_if_prefixes` exclusions unchanged.

**Tests** (`tests/unit/` — a new `test_orchestrator_heuristics.py` or extend the
existing orchestrator tests; eval phrasings ARE allowed in *tests* — the rule
bans them from engine code and prompts, and regression tests must prove the old
inputs still trigger):

- Triggers: "Is 246 even or odd? …", "Is Sydney, Australia in the northern or
  southern hemisphere? If southern, …", "Is 97 prime or composite? If prime …"
  (novel domain — proves generalisation), "Check whether the file exists,
  otherwise create it."
- Does not trigger: "Compare TCP vs UDP for this and recommend one.",
  "Summarise the CAP theorem in three bullets.", "Compare Postgres, Redis, and
  Kafka … then recommend one."
- The existing integration test for the conditional structural retry
  (`tests/integration/test_orchestrator.py`) must pass unmodified.

## Part 2 — Cookbook keyword cleanup

**Rule (now locked in `CLAUDE.md`/`AGENTS.md`):** cookbook `keywords` may only
contain *task-shape* words (compare, rank, decide, branch, fetch, summarize…),
never domain nouns and never phrases copied from eval prompts.

**Files:** `src/glassrail/planner/cookbooks/*.json` (all six). Known removals
(re-check every file against the rule — the lists may have drifted):

- `conditional_branch.json`: remove `even`, `odd`, `northern`, `southern`,
  `hemisphere`. Add `classify` and `or not` (shape words) to preserve recall.
- `compare_aggregate.json`: remove `throughput`, `durability`, `latency`,
  `license`, `sql`. Keep `compare, versus, vs, aggregate, rank, best,
  recommend, trade-off(s), across, each`.
- `direct_answer.json`: remove `who owns`, `exactly one`, `does not own`. Keep
  `explain, define, summarize, summarise, what is, overview, logic, deduce,
  deduction`.
- `single_tool.json`, `subplan.json`, `web_research.json`: audit against the
  rule; tool-family words (`file`, `read`, `web`, `search`, `calendar`,
  `memory`) are acceptable because they correspond to registered tool names
  (the selection bonus already keys off registered tools).

**Tests:** update `tests/unit/test_planner_cookbook.py` selections that relied
on removed keywords to use shape words instead (e.g. a conditional request
phrased as a binary question selecting `conditional_branch` via `whether`/
`decide`/`classify`).

**Eval check:** after Parts 1–2, re-run
`suites/glassrail-openrouter` and `suites/node-capability-openrouter` (3
trials). The decision-pair tasks (`classify-*`, `hemisphere-*`,
`decision-correct-*`) must not regress — if they do, the structural signals
are too weak and must be strengthened *structurally* (never by re-adding the
vocabulary).

## Part 3 — Held-out suite

**New directory:** `eval-framework/suites/glassrail-heldout/` — real task
directories (not symlinks), same OpenRouter backend/judge configuration as
`suites/glassrail-openrouter/suite.toml` (copy and adapt the `[backend]`,
`[backend.env]`, and `[judge]` blocks; same models, `k = 3`).

**The iteration ban (write it into the suite's own README.md and as a comment
at the top of suite.toml):** this suite exists to estimate generalisation. It
is run only at gate/confirmation time. Engine prompts, cookbook keywords, and
heuristics are **never** tuned against a failure in this suite. If a held-out
task must be studied to debug a failure, move it into the main `glassrail`
suite and write a replacement held-out task.

**Tasks** (~12; same capability areas and criteria rubric as
`eval-framework/suites/glassrail/EVAL_PLAN.md` — deterministic-first, control
pairs where the rubric demands, the two cross-cutting smoke criteria on every
task; reference-solve each prompt before shipping):

| id | area | mirrors | sketch |
|---|---|---|---|
| `heldout-classify-prime` | B | classify-even | "Is 97 prime or composite? If prime, state the largest prime smaller than it; if composite, give its smallest prime factor." D: regex `\bprime\b`, regex `\b89\b`; T: decision node; L: judge. |
| `heldout-classify-composite` | B, control_for prime | classify-odd | Same shape with 91 (composite; smallest prime factor 7). |
| `heldout-branch-leap` | B (d4) | hemisphere-south | "Was the year 2000 a leap year in the Gregorian calendar? If yes, name which divisibility rule makes it one; if no, name the rule that excludes it." → yes, divisible-by-400. |
| `heldout-branch-noleap` | B (d4), control | hemisphere-north | Same with 1900 → no, century-not-divisible-by-400 rule. |
| `heldout-calibrate-known` | A | calibrate-known | "How many degrees do the interior angles of a triangle sum to?" → 180, no hedging. |
| `heldout-calibrate-unknowable` | A, control | calibrate-unknowable | "What will the exact closing price of gold be tomorrow?" → declines to fabricate. |
| `heldout-recommend-queue` | C | recommend-datastore-iot | Distributing retry-safe background jobs to many workers: message queue vs shared DB table → queue, with rationale tied to stated constraints. |
| `heldout-recommend-table` | C, control | recommend-datastore-oltp | Constraints flipped (strict per-record audit history, tiny volume, transactional updates) → DB table. |
| `heldout-tool-read` | E | tool-read-value | Fixture `/tmp/glassrail-eval/service.toml` containing `retry_limit = 7`; "What retry limit is the service configured with? Config at …" D: `\b7\b`; T: file_read with target. |
| `heldout-tool-skip` | E, control | tool-skip-read | "What is the default SSH port?" → 22 from knowledge; T: file_read absent. |
| `heldout-summarize` | D | summarize-doc | New fixture doc with planted needles (a full name, a percentage, a date) + one distractor; D regex needles present, not_regex a plausible-but-absent fact. |
| `heldout-reason-chain` | F | reason-multistep-calc | New chained unit-conversion/arithmetic problem with one correct final number. |

**Reporting:** the roadmap's gate table gains a `glassrail-heldout` row. The
main-suite and held-out numbers are always published together; a large gap
between them is the overfitting signal and becomes its own ratchet item.

## Part 4 — Mechanics regression wall in CI

**File:** `.github/workflows/ci.yml`, the existing `eval-framework` job.

Add, after the current compile/dry-run steps:

```yaml
      - name: Install project for exec-plan subject
        run: uv sync --locked
      - name: Run harness-mechanics regression suite (scripted, no model calls)
        run: uv run python3 eval-framework/run.py suite eval-framework/suites/harness-mechanics
```

Notes for the implementer: the suite's subject runs `glassrail exec-plan`
as a subprocess — `uv run` puts the project venv (and so the `glassrail`
entry point) on `PATH`. Verify the suite's `[backend]` command resolution in
`eval-framework/suites/harness-mechanics/suite.toml` and
`evalkit/subjects/glassrail_exec_plan.py`; if the command is not found on
PATH, set `[backend] command` in the suite to `["glassrail", "exec-plan"]`
explicitly. The harness exits `1` when any regression task fails, which fails
the job — that is the point. Wall time is ~10–20 s.

## Part 5 — Use the promotion ratchet

After Parts 1–3 land and one clean confirmation run exists:

1. Run `suites/glassrail-openrouter` until the d1–d2 tasks and their controls
   have the 5 consecutive clean runs the ratchet requires
   (`promotion_threshold = 5` in suite.toml).
2. Promote them: `python3 eval-framework/run.py promote <task>` (the ratchet
   verifies candidacy; do not `--force`).
3. From then on the promoted regression set is part of the gate: any
   `pass^k < 1.0` on a regression task blocks release (exit code 1 already
   encodes this).

## Acceptance criteria

```bash
# No eval vocabulary left in engine code or cookbooks:
! grep -rn "even or odd\|odd or even\|northern or southern\|southern or northern" src/
! grep -in "hemisphere\|northern\|southern" src/glassrail/planner/cookbooks/*.json
! grep -in "throughput\|durability\|license\|sql" src/glassrail/planner/cookbooks/*.json
# Mechanics wall green locally and required in CI:
uv run python3 eval-framework/run.py suite eval-framework/suites/harness-mechanics
# Held-out suite loads and dry-runs:
python3 eval-framework/run.py list eval-framework/suites/glassrail-heldout
python3 eval-framework/run.py suite eval-framework/suites/glassrail-heldout --dry-run
```

Plus: the full check sweep; `glassrail-openrouter` and
`node-capability-openrouter` confirmation runs at or above their current
results; both suites' and the held-out suite's numbers recorded in the roadmap
gate table.
