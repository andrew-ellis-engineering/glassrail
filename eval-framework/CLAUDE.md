# Working in eval-framework/

A self-contained, multi-trial evaluation harness for AI skills: it runs each
task k times via `claude -p`, captures output / side-effects / tool trajectory,
grades with a deterministic → trajectory → LLM cascade, and reports pass@k
(capability) vs pass^k (reliability). It is **vendored** into this repo but
stands alone — it does not import from `dagagent` and is excluded from the
package's ruff / pyright / pytest scope.

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
python3 run.py list suites/example                       # loads + summarizes
python3 run.py task suites/example/tasks/hello-known --dry-run   # zero-cost wiring check
python3 -c "from evalkit.stats import pass_at_k; assert pass_at_k(3,1,3)==1.0"
python3 -m compileall -q evalkit run.py                  # syntax check
```

A live `python3 run.py suite suites/example` needs the `claude` CLI and spends
model usage — see Cost discipline before running broadly.

## Cost discipline

Trials are `claude -p` calls; usage ≈ `tasks × trials × (1 + #llm criteria)`.
On a Claude **subscription** these draw down usage limits (the printed
`total_cost_usd` is an estimate, not a charge); on an **API key** they are real
dollars. Keep it cheap: default to `haiku` (the example suite does), keep
criteria deterministic (graders that aren't `llm` make no model calls), use
`run.py score` to re-grade archived trials for free, and `--dry-run` to
validate. Reserve sonnet/opus and higher `--trials` for real measurements.

## Layout

```
run.py            CLI: task · suite · list · score · promote · demote · candidates
evalkit/
  config.py       HARNESS_VERSION, paths, defaults
  models.py       dataclasses (Task, Trial, Score, …)
  loader.py       TOML → model
  claude.py       `claude -p` invocation (clean env)
  runner.py       fixtures backup/install/restore, invocation, evidence capture
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
- **New task:** a directory with `config.toml` + `prompt.md`. Start every task
  as `type = "capability"`; promote to `regression` only via the ratchet after
  proven stability. Put no grading hints in `prompt.md`.

## Commits

Follow the repo-wide style in `../CLAUDE.md`: plain prose, short body, no
`Co-Authored-By` trailer, no internal phase names.
