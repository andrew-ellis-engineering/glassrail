# baseline-raw

Raw-model baseline copied from `eval-framework/suites/glassrail/tasks` at
commit `65b4a2183a00bf39dd4aa33c58a8aa44c8b9b9ea`.

This suite is regenerated from the source Glassrail suite when the source tasks
change; do not hand-evolve the copied tasks. Generation removes trajectory
criteria because the raw model has no plan, decision, result, or tool trajectory.
Deterministic and LLM answer-quality criteria are retained.

Run with:

```bash
python3 run.py suite suites/baseline-raw --workers 5
```
