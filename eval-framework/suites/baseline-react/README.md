# baseline-react

ReAct-loop baseline copied from `eval-framework/suites/glassrail/tasks` at
commit `65b4a2183a00bf39dd4aa33c58a8aa44c8b9b9ea`.

This suite is regenerated from the source Glassrail suite when the source tasks
change; do not hand-evolve the copied tasks. Generation removes Glassrail-
mechanism trajectory criteria such as decision/result/subplan checks. Positive
`file_read` usage criteria are retained because the ReAct-loop subject exposes
the same local file-read tool.

Run with:

```bash
python3 run.py suite suites/baseline-react --workers 5
```
