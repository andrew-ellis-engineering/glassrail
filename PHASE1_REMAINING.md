# Phase 1 — Remaining Work

All five implementation items are complete. The only open gate before the
Phase 1 PyPI publish is the **eval promotion ratchet**.

---

## Exit gate: eval promotion

Promote capability tasks to regression once they hit the agreed pass^k bar
across a clean eval run (provider healthy, no timeout-driven failures).

Steps:
1. Run `python3 eval-framework/run.py suite eval-framework/suites/dagagent` with
   a healthy provider.
2. Identify tasks at 5/5 trials (pass^k = 1.0).
3. For each, run `python3 eval-framework/run.py promote <suite> <task>` to raise
   them to `type = "regression"`.
4. Agree on a pass^k floor (e.g. 80%) for the full regression suite before
   cutting the first PyPI release.

Known gaps that block some tasks from promotion regardless of provider health:
- `subplan-correct` — planner rarely emits subplans naturally; needs prompt work
  or more data before this reliably hits pass^k = 1.0.
- `hemisphere-north` — genuine model reasoning gap; the local model places Madrid
  seasonality incorrectly every trial.
- `classify-odd` / `decision-correct-no` / `think-intermediate-correct` — node-shape
  criteria tension: correct answers but wrong node type. Decide whether to tighten
  the planner prompt or relax the trajectory criteria before promoting.
