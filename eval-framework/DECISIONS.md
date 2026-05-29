# Decisions

Non-obvious calls made while building from `EVAL_FRAMEWORK_SPEC.md`. The spec's
prompt said to make reasonable choices and record them here rather than ask.

1. **Build location — standalone `~/eval-framework/`.** The spec frames the
   framework as self-contained ("root of an empty directory") and generic
   (evaluates any AI skill via `claude -p`), so it lives in its own directory
   rather than inside another project. Move it anywhere; nothing is hard-coded
   to this path (`config.FRAMEWORK_ROOT` derives from the package location).

2. **Added a `file_unchanged` deterministic check.** The Cookbook (recipe 5,
   read-only advisory skills) uses it, but it isn't in the Rebuild Spec's check
   table. To support it without re-reading live state at grade time, `Trial`
   gained a `baseline` field — a snapshot of each `capture` path taken *before*
   the run. `file_unchanged` passes when the post-run content equals the
   baseline. This keeps grading decoupled from the environment (principle 7).

3. **Trajectory capture depends on the envelope shape.** Per the spec, the
   runner extracts `tool_use` blocks from `messages[].content[]` in the
   `claude -p --output-format json` output. If a given CLI version's envelope
   doesn't include the message list, trajectory degrades gracefully to empty
   rather than erroring. Trajectory criteria should be validated against your
   installed `claude` version; the example suite uses none.

4. **LLM cost gate defaults ON.** Principle 5 calls it "configurable": when a
   task has deterministic criteria and none pass, the trial is already a fail,
   so LLM judges are skipped (recorded as failed with an explicit reason).
   Pass `cost_optimize=False` to the dispatcher to force judging.

5. **`pass@k` is reported with k = trials run.** Since we run exactly k=n
   trials, the Chen estimator yields 0 or 1 (capability: did any trial pass?).
   The fractional reliability signal lives in `pass^k`, and the Wilson CI is
   computed on the pass^k proportion (perfect trials / trials).

6. **No third-party deps for stats.** Wilson CI needs a normal critical value;
   with no SciPy, `z` is found by bisection on `math.erf` (exact to ~1e-9).

7. **`promote` is candidate-gated.** It refuses unless the task has the
   required consecutive clean runs, matching the human-gated ratchet
   (principle 9). `--force` overrides. `demote` removes the promotion fields
   and records `demotion_reason` + `demoted_at`.

8. **LLM judge fails closed.** Only a leading `PASS` passes; `FAIL`, `UNKNOWN`,
   unparseable output, or an invocation error all count as not-passed.
