# Evals

Evals answer a different question from the unit and integration tests: not
"does this function behave?" but "given a request, does the agent plan and
execute it *well, and reliably*?" They are model-dependent and non-deterministic,
so they are measured with multiple trials — not asserted once.

Evals live in the vendored [`eval-framework/`](https://github.com/andrew-ellis-engineering/glassrail/tree/main/eval-framework):
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
| `glassrail-cli` | `glassrail run --json` (subprocess) | eval the real planner + executor over **your** tier routing |
| `glassrail-gateway` | a running REST gateway over HTTP | eval the deployed surface end to end |
| `openai-compat` | one `/chat/completions` call | baseline the raw model with no agent scaffolding |
| `react-loop` | OpenAI-compatible chat with a local `file_read` tool loop | baseline a conventional tool-calling loop |
| `claude-cli` | `claude -p` | eval a Claude Code skill (the framework's original target) |

The glassrail backends route through the agent's **own** tier config, so they
benchmark the model(s) you actually deploy — tier 0 is your local MLX server by
default. There is no point benchmarking against a model you will not run: point
your tiers at the shipped model (`config.toml` / `GLASSRAIL_TIER*__…` env), or
pass a tier-0 override via the suite's `default_model`.

## Running

```bash
cd eval-framework
python3 run.py list suites/glassrail                 # load + summarize
python3 run.py suite suites/glassrail --dry-run       # zero-cost wiring check
python3 run.py suite suites/glassrail --trials 5      # the real thing (needs MLX up)
```

A real run needs the agent reachable: `glassrail` on `PATH` (activate the venv,
or set the suite's `[backend] command = ["uv", "run", "glassrail", "run"]`) and
your tier-0 MLX server serving the model. The judge is independent of the
subject. Local suites default to a cheap Claude CLI judge; OpenRouter mirror
suites default to `anthropic/claude-haiku-4.5` through OpenRouter so grading
does not draw down Claude Code subscription usage. You can override either with
`--grader-backend` and `--grader-model`.

## The `glassrail run --json` contract

The `glassrail-cli` backend shells out to a headless run and reads one JSON
envelope from stdout:

```bash
glassrail run "Is 246 even or odd?" --json --model qwen3.6-35b-moe
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

Trial artifacts also carry `total_tokens` when the subject exposes it. Suite
summaries print mean tokens per task so raw-model, ReAct-loop, and Glassrail
runs can be compared on economics as well as pass@k/pass^k.

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

## Running against cloud models (OpenRouter)

Local MLX evals take hours per iteration — cold boot, model load, and
generation are all slow. When you need a faster signal (ruling out infra
issues, iterating on tasks, running CI without Apple Silicon), the repo ships
two OpenRouter-backed mirror suites that run the same tasks against the same
Qwen models served from the cloud:

| Suite | Mirror of | Typical wall time |
|---|---|---|
| `suites/glassrail-openrouter` | `suites/glassrail` | a few minutes for 20+ tasks |
| `suites/node-capability-openrouter` | `suites/node-capability` | ~2 min for 7 tasks |

**One-time setup — API key:**

```bash
# Add to ~/.zshenv so it's available to subject and judge subprocesses
echo 'export OPENROUTER_API_KEY="sk-or-..."' >> ~/.zshenv
source ~/.zshenv
```

**Running:**

```bash
cd eval-framework
python3 run.py suite suites/glassrail-openrouter --workers 5
python3 run.py suite suites/node-capability-openrouter --workers 5

# Or both cloud gate suites at once:
python3 run.py suite suites/glassrail-openrouter --workers 5 && \
python3 run.py suite suites/node-capability-openrouter --workers 5
```

To benchmark model choices without editing suite files, use tier-specific
overrides on Glassrail suites. `--model` still means the subject model and, for
`glassrail-cli`, the tier-0 shorthand; `--tier-model N=MODEL` overrides the
model configured for any Glassrail tier:

```bash
python3 run.py suite suites/glassrail-openrouter \
  --tier-model 0=deepseek/deepseek-v4-flash \
  --tier-model 1=deepseek/deepseek-v4-pro \
  --workers 5
```

**How it works:** each suite's `[backend.env]` sets all four `GLASSRAIL_TIER*`
vars to OpenRouter endpoints, overriding the local config. The `glassrail-cli`
subject maps `OPENROUTER_API_KEY` into the per-tier API-key variables, and the
LLM judge reads the same key directly from suite `[judge]` config. The suite
also sets
`GLASSRAIL_MAX_GENERATION_TOKENS=32768` (the local config caps this at 8192 for
Metal OOM safety; cloud has no such constraint) and passes
`reasoning.effort=none` with `provider.require_parameters=true` via
`GLASSRAIL_TIER*__EXTRA_BODY` — required because Qwen3 models on OpenRouter
default to extended thinking mode, which streams all tokens into
`delta.reasoning` and leaves `delta.content` empty.
Tier-model CLI overrides are layered on top of that suite env immediately before
the subject process is spawned, so they do not change the checked-in suite
defaults.

**Cost:** OpenRouter charges per token. A full `glassrail-openrouter` run at
default `--trials 3` now includes both the Qwen subject calls and Haiku 4.5
judge calls. Keep `--workers` at 5 or below to avoid rate-limit 429s.

**When to use each:**

- **Cloud first** for any iteration cycle under a day — local models take
  12+ hours for a meaningful run.
- **Local for final gates** — the cloud suites use the same model family but
  not the same inference stack as production; use them for signal, not as the
  ship-gate.
- **Cloud to isolate infra vs model quality** — if cloud passes and local
  fails the same task, the failure is in the serving stack, not the model.

## Comparative Baselines

The launch-evidence suites compare the same Glassrail task prompts under the
same OpenRouter subject model and judge:

| Suite | Subject | Criteria changes |
|---|---|---|
| `suites/baseline-raw` | one raw `openai-compat` completion | Glassrail trajectory criteria removed |
| `suites/baseline-react` | `react-loop` with local `file_read` | Glassrail trajectory criteria removed; positive `file_read` checks retained |
| `suites/glassrail-openrouter` | full Glassrail planner/executor | original criteria |

Run the comparison after setting `OPENROUTER_API_KEY`:

```bash
cd eval-framework
python3 run.py suite suites/baseline-raw --workers 5 && \
python3 run.py suite suites/baseline-react --workers 5 && \
python3 run.py suite suites/glassrail-openrouter --workers 5
```

Publish the resulting table with suite, pass@3, pass^3, and mean tokens/task.
State the date and harness version from `run_metadata.json`; if a baseline wins
on any dimension, keep it in the table.

## Cost discipline

Trials cost real inference. Keep criteria deterministic where possible (only
`llm` criteria call a model), default the judge to a cheap model, re-grade
archived trials for free with `run.py score`, and validate wiring with
`--dry-run` before a broad run. See `eval-framework/README.md`'s cost section.
