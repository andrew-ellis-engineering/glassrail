# Evals

Evals answer a different question from the unit and integration tests: not
"does this function behave?" but "given a request, does the agent plan and
execute it *well, and reliably*?" They are model-dependent and non-deterministic,
so they are measured with multiple trials — not asserted once.

Evals live in the vendored [`eval-framework/`](https://github.com/andrewellis/dagagent/tree/main/eval-framework):
a standalone, stdlib-only harness that runs each task *k* times, captures the
output / trajectory / side-effects, grades with a deterministic → trajectory →
LLM cascade, and reports **pass@k** (capability — can it ever?) vs **pass^k**
(reliability — does it every time?). See `eval-framework/README.md` for the full
manual and `eval-framework/CLAUDE.md` for its operating constraints.

## Pluggable subjects — benchmark the model you ship

The harness is backend-agnostic. A "subject" is the system under test; every
backend returns the same normalized result, so the graders never change:

| backend | what it runs | use it to… |
|---|---|---|
| `dagagent-cli` | `dagagent run --json` (subprocess) | eval the real planner + executor over **your** tier routing |
| `dagagent-gateway` | a running REST gateway over HTTP | eval the deployed surface end to end |
| `openai-compat` | one `/chat/completions` call | baseline the raw model with no agent scaffolding |
| `claude-cli` | `claude -p` | eval a Claude Code skill (the framework's original target) |

The dagagent backends route through the agent's **own** tier config, so they
benchmark the model(s) you actually deploy — tier 0 is your local MLX server by
default. There is no point benchmarking against a model you will not run: point
your tiers at the shipped model (`config.toml` / `DAGAGENT_TIER*__…` env), or
pass a tier-0 override via the suite's `default_model`.

## Running

```bash
cd eval-framework
python3 run.py list suites/dagagent                 # load + summarize
python3 run.py suite suites/dagagent --dry-run       # zero-cost wiring check
python3 run.py suite suites/dagagent --trials 5      # the real thing (needs MLX up)
```

A real run needs the agent reachable: `dagagent` on `PATH` (activate the venv,
or set the suite's `[backend] command = ["uv", "run", "dagagent", "run"]`) and
your tier-0 MLX server serving the model. The judge is independent of the
subject — it defaults to a cheap Claude model so semantic criteria grade
consistently; point it at MLX instead with `--grader-backend openai-compat`.

## The `dagagent run --json` contract

The `dagagent-cli` backend shells out to a headless run and reads one JSON
envelope from stdout:

```bash
dagagent run "Is 246 even or odd?" --json --model qwen3.6-35b-moe
```

```json
{
  "result": "246 is even; half of it is 123.",
  "trajectory": [{"tool": "decision", "node_type": "decision", "tier_used": 0,
                  "branch_taken": "even", "confidence": 0.9, ...}, ...],
  "status": "completed", "is_error": false, "error": null,
  "total_cost_usd": null, "total_tokens": 412, "task_id": "01J…"
}
```

`result` is the task's `final_output`; `trajectory` normalizes the executed plan
(tool nodes → the tool name, every other node → its type, with tier and branch
recorded per step) so trajectory criteria work the same across backends. Logs go
to stderr, so stdout stays a clean envelope.

## Anatomy of a task

A task is a directory under `suites/<name>/tasks/<id>/` with a `prompt.md` (sent
to the agent — **no grading hints**, the temporal firewall) and a `config.toml`
declaring criteria:

```toml
type = "capability"          # start here; the ratchet promotes to "regression"
difficulty = 2
control_for = "classify-even" # paired opposite-answer task (concordance check)

[[criteria]]                  # deterministic first — 100% precision, no model
text = "Identifies 247 as odd"
grader = "deterministic"
check = "regex"
target = "__result_text__"
value = "(?i)\\bodd\\b"

[[criteria]]                  # trajectory — did it use the mechanism?
text = "Planner used a decision node to branch on the condition"
grader = "trajectory"
tool_sequence = ["decision"]

[[criteria]]                  # llm judge — last resort, one dimension per call
text = "Correctly classifies as odd and gives the next even number as 248"
grader = "llm"
```

Branch *labels* are planner-chosen and unstable, so branch correctness is graded
on the observable result text (or the judge), not on the trajectory token — the
token for any decision node is simply `decision`.

## Cost discipline

Trials cost real inference. Keep criteria deterministic where possible (only
`llm` criteria call a model), default the judge to a cheap model, re-grade
archived trials for free with `run.py score`, and validate wiring with
`--dry-run` before a broad run. See `eval-framework/README.md`'s cost section.
