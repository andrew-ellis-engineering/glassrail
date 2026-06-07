# Working in eval-framework/

A self-contained, multi-trial evaluation harness for agent workflows: it runs
each task k times against a pluggable **subject** (the system under test),
captures output / side-effects / trajectory, grades with a deterministic →
trajectory → LLM cascade, and reports pass@k (capability) vs pass^k
(reliability). It is **vendored** into this repo but stands alone — it does
**not** import from `dagagent` (it reaches the agent over a subprocess / HTTP
boundary, like it reaches `claude -p`) and is excluded from the package's ruff /
pyright / pytest scope.

Backends live in `evalkit/subjects/`: `dagagent-cli` and `dagagent-gateway`
(the real agent), `openai-compat` (a raw model, e.g. MLX), and `claude-cli`. The
judge (the `llm` grader) is decoupled from the subject — see `evalkit/judge.py`.

Read `README.md` for usage and `DECISIONS.md` for build-time choices before
changing anything.

## Hard constraints (locked — don't relitigate)

- **Python 3.11+ standard library ONLY.** No third-party dependencies — not
  pydantic, not pytest, not requests, not tomli (use stdlib `tomllib`). The
  data model is plain `dataclasses`. Adding a dependency is a design change,
  not a convenience.
- **The 10 principles are non-negotiable** and are enforced by architecture,
  not convention (see `README.md`). In particular: deterministic graders
  first; multi-trial mandatory; the agent never sees grading criteria
  (temporal firewall); trajectory is first-class evidence; trials are
  re-gradable offline; the harness is versioned.
- **Bump `HARNESS_VERSION`** in `evalkit/config.py` on any behavioral change to
  running or grading — every trial stamps it, and results across versions are
  not comparable.
- **Safety-critical checks stay deterministic.** Never put a "did it avoid the
  forbidden action" check on the LLM judge.

## Run & validate

There is no unit-test suite or lint/type config here; validate by running.

```bash
python3 run.py list suites/dagagent                      # loads + summarizes
python3 run.py suite suites/dagagent --dry-run           # zero-cost wiring check
python3 -c "from evalkit.stats import pass_at_k; assert pass_at_k(3,1,3)==1.0"
python3 -m compileall -q evalkit run.py                  # syntax check
```

A live `python3 run.py suite suites/dagagent` needs the agent reachable
(`dagagent` on PATH + your MLX tier up) and spends model usage; the `example`
suite needs the `claude` CLI. See Cost discipline before running broadly.

## Running against OpenRouter instead of local MLX

Local inference takes 12+ hours for a meaningful eval cycle. When local
servers are down or you need fast iteration, use the cloud mirror suites —
same tasks, same models served via OpenRouter:

```bash
# One-time: export your key (add to ~/.zshenv so subprocesses see it)
export DAGAGENT_TIER0__API_KEY=$OPENROUTER_API_KEY
export DAGAGENT_TIER1__API_KEY=$OPENROUTER_API_KEY
export DAGAGENT_TIER2__API_KEY=$OPENROUTER_API_KEY
export DAGAGENT_TIER3__API_KEY=$OPENROUTER_API_KEY

python3 run.py suite suites/dagagent-openrouter --workers 5
python3 run.py suite suites/node-capability-openrouter --workers 5
```

The suites live in `suites/dagagent-openrouter/` and
`suites/node-capability-openrouter/`. Their `[backend.env]` blocks handle
everything: tier URLs, model slugs, `DAGAGENT_MAX_GENERATION_TOKENS`,
and the Qwen3-on-OpenRouter reasoning fix (`reasoning.effort=none` +
`provider.require_parameters=true` via `EXTRA_BODY`). Do not add
`:no-thinking` model suffixes — they are routing hints only and do not
guarantee reasoning is disabled on all providers; the `EXTRA_BODY` parameter
is the authoritative control.

**Qwen3 / OpenRouter integration notes** (captured here to avoid re-learning):

- Qwen3 models on OpenRouter default to extended thinking. All tokens go to
  `delta.reasoning`; `delta.content` is empty. The agent's SSE parser reads
  only `delta.content`, so an unmitigated thinking response looks like an
  empty reply and fails with `Expecting value: line 1 column 1 (char 0)`.
- The correct fix is `"reasoning":{"effort":"none"}` in the request body,
  passed via `DAGAGENT_TIER*__EXTRA_BODY`. You must also set
  `"provider":{"require_parameters":true}` — without it OpenRouter may route
  to a provider that ignores the parameter and silently re-enables thinking.
- The `is_healthy()` pre-flight check hits `<base_url_root>/health`. Cloud
  providers (OpenRouter, etc.) return an HTML 200, not JSON. The provider
  handles this gracefully (treats non-JSON 200 as available), but local-only
  servers return `{"status":"healthy"}` as expected.
- `total_tokens` in the run envelope counts execution-node tokens only.
  Planner token counts live in `planning_attempts[*].tokens_used`.

## Cost discipline

Each trial is one subject invocation plus one call per `llm` criterion; usage ≈
`tasks × trials × (1 + #llm criteria)`. Where it lands depends on the backend:
`dagagent-*` / `openai-compat` hit your own infra (local MLX = no per-token
dollars; tokens travel in the envelope); the judge defaults to Claude — on a
**subscription** it draws down usage limits (printed `total_cost_usd` is an
estimate, not a charge), on an **API key** it's real dollars. Keep it cheap:
prefer deterministic criteria (only `llm` calls a model), keep the judge on a
cheap model (or point it at MLX with `--grader-backend openai-compat`), use
`run.py score` to re-grade archived trials for free, and `--dry-run` to validate.

## Layout

```
run.py            CLI: task · suite · list · score · promote · demote · candidates
evalkit/
  config.py       HARNESS_VERSION, paths, defaults
  models.py       dataclasses (Task, Trial, Score, …)
  loader.py       TOML → model (resolves backend + backend_config)
  subjects/       Subject seam + backends (claude_cli, dagagent_cli,
                  dagagent_gateway, openai_compat) + build_subject
  judge.py        the LLM judge (backend-agnostic) + build_judge
  runner.py       fixtures backup/install/restore, subject invocation, capture
  graders/        deterministic · trajectory · llm  (+ __init__ dispatcher)
  stats.py        pass@k (Chen), pass^k, Wilson CI
  reporter.py     tables + artifact save/load
  ratchet.py      promotion-candidate detection + surgical TOML edits
suites/<name>/    suite.toml + tasks/<id>/{config.toml, prompt.md} [+ fixtures/]
results/          trial artifacts (gitignored)
```

## Extending

- **New deterministic check:** add a branch in `graders/deterministic.py` and
  document its semantics in `README.md`'s check table.
- **New node of the grading cascade:** keep the det → traj → llm ordering in
  `graders/__init__.py`; LLM stays last and one-dimension-per-call.
- **New backend (subject):** add a class in `evalkit/subjects/` that returns a
  `RunResult`, register it in `subjects/__init__.py`. Reach the system over a
  process / HTTP boundary — never `import dagagent` (the stdlib-only constraint).
- **New task:** a directory with `config.toml` + `prompt.md`. Start every task
  as `type = "capability"`; promote to `regression` only via the ratchet after
  proven stability. Put no grading hints in `prompt.md`.

## Commits

Follow the repo-wide style in `../CLAUDE.md`: plain prose, short body, no
`Co-Authored-By` trailer, no internal phase names.
