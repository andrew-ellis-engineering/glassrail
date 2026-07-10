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

5. **`pass@k` is reported with k = trustworthy graded trials.** Infrastructure
   failures and `--skip-grading` placeholders are excluded. Since we evaluate
   exactly k=n eligible trials, the Chen estimator yields 0 or 1 (capability:
   did any valid trial pass?). The fractional reliability signal lives in
   `pass^k`, and the Wilson CI is computed on eligible perfect trials / eligible
   trials. Metrics are unavailable when no eligible trials remain.

6. **No third-party deps for stats.** Wilson CI needs a normal critical value;
   with no SciPy, `z` is found by bisection on `math.erf` (exact to ~1e-9).

7. **`promote` is candidate-gated.** It refuses unless the task has the
   required consecutive clean runs, matching the human-gated ratchet
   (principle 9). `--force` overrides. `demote` removes the promotion fields
   and records `demotion_reason` + `demoted_at`.

8. **LLM judge fails closed.** Only a leading `PASS` passes; `FAIL`, `UNKNOWN`,
   or unparseable judge output count as model-quality not-passed. A judge
   invocation failure is infrastructure-invalid and is excluded from metrics.

9. **Subject abstraction (v0.2.0) — the framework no longer assumes `claude -p`.**
   The system under test is now a pluggable *subject* behind one normalized
   `RunResult` (`evalkit/subjects/`); `claude -p` is just `claude-cli`, alongside
   `glassrail-cli`, `glassrail-gateway`, and `openai-compat`. The runner and
   graders work off `RunResult`/`Trial` evidence only, so they are
   backend-agnostic. To honor the stdlib-only constraint *and* stay decoupled,
   every subject reaches its system over a process or HTTP boundary — the
   framework never imports `glassrail`. The **judge** is likewise decoupled from
   the subject (`evalkit/judge.py`): you can benchmark a local MLX model while
   judging with Claude. A suite selects its backend via `default_backend` +
   an optional `[backend]` config table; `HARNESS_VERSION` was bumped to `0.2.0`
   (results across that boundary are not comparable).

10. **glassrail trajectory tokens.** The `glassrail-*` backends map an
    ExecutionState into the trajectory vocabulary: tool nodes → the tool name,
    every other node → its type. Decision nodes are just `decision` (branch
    labels are planner-chosen and unstable); the branch taken lives in the
    step's `branch_taken` field, so branch correctness is graded on the
    observable result text, not on a fragile token match.

11. **glassrail CLI failure envelopes are model failures, not infra failures.**
    From harness v0.3.9, `glassrail run --json` and `glassrail exec-plan --json`
    may exit nonzero after printing a parseable envelope. The subject treats
    those envelopes as normal failed trials (`success = false`) and leaves the
    structured envelope attached for grading/reporting. A nonzero exit with
    unparseable stdout remains an infrastructure error.

12. **Comparative baselines report tokens, not latency.** From harness v0.4.0,
    subjects may return `total_tokens`, trials persist it, and suite summaries
    show mean tokens per task. The `react-loop` backend is deliberately a
    plain OpenAI-compatible tool loop with only a local `file_read` tool, so
    raw-model, ReAct-loop, and Glassrail runs can be compared on the same
    answer-quality criteria without importing Glassrail into the harness.

13. **Infrastructure failures are not model outcomes.** From harness v0.5.0,
    subjects explicitly distinguish invocation failures from parseable model
    failure envelopes. Infrastructure-error trials are archived and reported
    but excluded from quality metrics and regression gates. The invalid-run
    tripwire is inclusive (`rate >= INVALID_RUN_INFRA_RATE`). Suite re-grading
    is staged in memory: an invalid re-grade writes nothing, while a valid one
    replaces score/task metadata and refreshes run scoring metadata together.
