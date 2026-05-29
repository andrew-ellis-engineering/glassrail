# AI Skill Eval Framework

A multi-trial evaluation framework for AI skills (slash commands, agent
workflows). It runs each task **k times** via `claude -p`, captures three
evidence channels (output, side-effects, tool trajectory), grades with a
**deterministic → trajectory → LLM** cascade, and reports **pass@k**
(capability) vs **pass^k** (reliability).

Python 3.11+ stdlib only — no third-party dependencies. Requires the `claude`
CLI on `PATH` for live runs.

## Quick start

```bash
# Validate a suite loads and summarize it
python3 run.py list suites/example

# See what a run would do without spending inference
python3 run.py task suites/example/tasks/hello-known --dry-run --trials 3

# Run a suite for real (needs `claude`); writes results/<run>/…
python3 run.py suite suites/example --trials 3 --timeout 60

# Re-grade archived trials with current criteria — zero inference cost
python3 run.py score results/<run>/hello-known
```

## Cost & limits

Every trial is one `claude -p` call, so the model usage of a run is roughly:

```
tasks × trials × (1 generation  +  #llm-judge criteria)
```

How that's billed depends on how `claude` is authenticated:

- **Logged in with a Claude subscription (claude.ai OAuth):** runs draw down
  your plan's **usage limits** (rolling window + weekly caps), not a dollar
  balance. The `total_cost_usd` each run prints is an *equivalent-API-cost
  estimate* for reference — it is not a charge.
- **API key (Anthropic Console):** `total_cost_usd` is a real, pay-as-you-go
  charge. Set spend limits / alerts in the Console.

Keep runs cheap:

- **Model is the big lever** — haiku ≪ sonnet < opus. The example suite
  defaults to `haiku`; bump to sonnet/opus only for a real measurement
  (`--model sonnet`). Set a suite's floor in `suite.toml` (`default_model`).
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
  claude.py            `claude -p` invocation (clean env)
  runner.py            fixtures, invocation, evidence capture
  graders/             deterministic · trajectory · llm + dispatcher
  stats.py             pass@k (Chen), pass^k, Wilson CI
  reporter.py          tables + artifact save/load
  ratchet.py           promotion-candidate detection + TOML edits
suites/example/        a paired calibration suite
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

Common flags: `--trials N`, `--model`, `--grader-model`, `--timeout S`,
`--dry-run`, `--skip-grading`, `--run-name`.

**Exit codes:** `0` success · `1` a regression task scored pass^k = 0 (CI
gating signal) · `2` framework error.

## Writing tasks

A task is a directory with `config.toml` (metadata + criteria) and `prompt.md`
(what the agent receives — no grading hints). See `suites/example/` for a
control pair, and the cookbook for recipes. New tasks start as
`type = "capability"`; promote to `regression` only after proven stability.

See `DECISIONS.md` for build-time choices.
