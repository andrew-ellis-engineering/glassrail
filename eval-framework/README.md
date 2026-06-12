# Eval Framework

A multi-trial evaluation framework for agent workflows. It runs each task
**k times** against a pluggable **subject** (the system under test), captures
three evidence channels (output, side-effects, trajectory), grades with a
**deterministic → trajectory → LLM** cascade, and reports **pass@k**
(capability) vs **pass^k** (reliability).

Python 3.11+ stdlib only — no third-party dependencies, and it does not import
the host project: every subject is reached over a process or HTTP boundary.

## Backends (subjects)

A suite names a `backend`; all return the same normalized result, so graders
never change:

| backend | runs | for |
|---|---|---|
| `glassrail-cli` | `glassrail run --json` (subprocess) | the real planner + executor over the agent's own tier routing (your shipped model) |
| `glassrail-gateway` | a running REST gateway (HTTP) | the deployed surface, end to end |
| `openai-compat` | one `/chat/completions` call | a raw-model baseline (e.g. the local MLX server) |
| `react-loop` | OpenAI-compatible chat with a local `file_read` tool loop guarded to `/tmp/glassrail-eval` regular files | a conventional tool-calling baseline |
| `claude-cli` | `claude -p` | a Claude Code skill (the original target) |

The **judge** (the `llm` grader) is decoupled from the subject — keep it on a
cheap model, point it at MLX, or point it at OpenRouter with
`--grader-backend openai-compat`. `claude-cli` backends need the `claude` CLI
on `PATH`; `glassrail-*` need the agent reachable.

## Quick start

```bash
# Validate a suite loads and summarize it
python3 run.py list suites/example

# See what a run would do without spending inference
python3 run.py task suites/example/tasks/hello-known --dry-run --trials 3

# Run the glassrail suite for real (needs the agent + MLX up); writes results/<run>/…
python3 run.py suite suites/glassrail --trials 5

# Run the claude-skill example suite instead
python3 run.py suite suites/example --trials 3 --timeout 60

# Re-grade archived trials with current criteria — zero inference cost
python3 run.py score results/<run>/classify-even
```

## Cost & limits

Each trial is one subject invocation plus one call per `llm` criterion, so the
model usage of a run is roughly:

```
tasks × trials × (1 generation  +  #llm-judge criteria)
```

Where that cost lands depends on the backend. The `glassrail-*` and
`openai-compat` backends hit the configured endpoint (local MLX, OpenRouter,
or another OpenAI-compatible service). The judge is separate and, by default,
runs on Claude for local suites and OpenRouter for the OpenRouter mirror suites.
Subjects that expose token usage populate `total_tokens`; reports show mean
tokens per task for baseline comparisons.

For a `claude-cli` subject (or a Claude judge), how it's billed depends on how
`claude` is authenticated:

- **Logged in with a Claude subscription (claude.ai OAuth):** runs draw down
  your plan's **usage limits** (rolling window + weekly caps), not a dollar
  balance. The `total_cost_usd` each run prints is an *equivalent-API-cost
  estimate* for reference — it is not a charge.
- **API key (Anthropic Console):** `total_cost_usd` is a real, pay-as-you-go
  charge. Set spend limits / alerts in the Console.

Keep runs cheap:

- **Model is the big lever** — for a Claude subject/judge, haiku ≪ sonnet < opus
  (the `example` suite defaults to `haiku`). The OpenRouter mirror suites grade
  with `anthropic/claude-haiku-4.5` by default, using `OPENROUTER_API_KEY`.
  For the `glassrail` backend the model is whatever your tiers serve;
  `default_model` / `--model` override tier 0, and `--tier-model N=MODEL`
  can pin any Glassrail tier for model-matrix runs.
- **Trials scale linearly** — `--trials 1` while iterating, `3` for a result.
- **Grading is mostly free** — deterministic and trajectory checks make **no**
  model calls. Only `grader = "llm"` criteria cost (one call each, on
  `--grader-model`), and they're skipped automatically when every
  deterministic criterion already failed. Prefer deterministic criteria
  (principle 1) and a cheap `--grader-model`.
- **`score` is free** — re-grade archived trials to calibrate criteria instead
  of re-running the suite. **`--dry-run`** validates wiring with zero calls.
- **`max_turns`** bounds the worst case — a looping agent burns the most.

## Principles (enforced by architecture, not convention)

1. Deterministic graders first; LLM judges last resort.
2. Multi-trial mandatory (k≥3); report pass@k **and** pass^k.
3. Temporal firewall — the agent never sees grading criteria.
4. Trajectory is first-class evidence.
5. Hybrid grading cascade (deterministic → trajectory → LLM).
6. One independent, reference-guided LLM judge per dimension.
7. Decouple generation from scoring — trials are re-gradable.
8. Control pairs — a paired task where the opposite answer is correct.
9. Capability vs regression separation, with a human-gated promotion ratchet.
10. The harness is versioned; every trial stamps `harness_version`.

The full rationale is in the methodology and cookbook docs this was built from.

## Layout

```
run.py                 CLI entry point
evalkit/
  config.py            HARNESS_VERSION, paths, defaults
  models.py            dataclasses (Task, Trial, Score, …)
  loader.py            TOML → model
  subjects/            the Subject seam + backends (claude_cli, glassrail_cli,
                       glassrail_gateway, openai_compat) + build_subject
  judge.py             the LLM judge (backend-agnostic), build_judge
  runner.py            fixtures, subject invocation, evidence capture
  graders/             deterministic · trajectory · llm + dispatcher
  stats.py             pass@k (Chen), pass^k, Wilson CI
  reporter.py          tables + artifact save/load
  ratchet.py           promotion-candidate detection + TOML edits
suites/glassrail/       end-to-end evals of the agent (glassrail-cli backend)
suites/example/        a paired claude-skill calibration suite
results/               trial artifacts (gitignored)
```

## CLI

| Command | What it does |
|---------|--------------|
| `task <path>` | Run one task k times, grade, save artifacts. |
| `suite <path>` | Run a whole suite. `--tags`, `--type` filter. |
| `list [<path>]` | Validate + summarize a suite (or list suites). |
| `score <results-path>` | Re-grade archived trials with current criteria (free). |
| `promote <task>` | Capability → regression (candidate-gated; `--force`). |
| `demote <task> --reason` | Regression → capability. |
| `candidates [<suite>]` | Show tasks eligible for promotion. |

Common flags: `--trials N`, `--backend`, `--model`, `--tier-model N=MODEL`,
`--tier0-model MODEL` ... `--tier3-model MODEL`, `--grader-backend`,
`--grader-model`, `--timeout S`, `--dry-run`, `--skip-grading`, `--run-name`.

For `glassrail-cli` suites, `--model` remains the tier-0 shorthand. Use
`--tier-model` when you need to vary planner/executor tiers independently:

```bash
python3 run.py suite suites/glassrail-openrouter \
  --tier-model 0=deepseek/deepseek-v4-flash \
  --tier-model 1=deepseek/deepseek-v4-pro \
  --workers 5
```

**Exit codes:** `0` success · `1` a regression task scored pass^k = 0 (CI
gating signal) · `2` framework error.

## Writing tasks

A task is a directory with `config.toml` (metadata + criteria) and `prompt.md`
(what the agent receives — no grading hints). See `suites/example/` for a
control pair, and the cookbook for recipes. New tasks start as
`type = "capability"`; promote to `regression` only after proven stability.

See `DECISIONS.md` for build-time choices.
