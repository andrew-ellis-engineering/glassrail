# dagagent eval plan

The exhaustive eval design for the dagagent suite, derived by applying the
[Eval Framework Methodology](../../../docs/evals.md) decision trees to
dagagent's behaviour surface. This is the *what to build and why*; `suite.toml`
+ the `tasks/` dirs are the *what is built*, and `docs/evals.md` is *how to run*.

The goal this plan serves is the **Phase 1 exit gate**: a regression set that
passes a `pass^k` bar, which is what unlocks the first PyPI publish.

---

## What we are evaluating

The "skill" under test is not a single function — it is *given a request, does
the agent plan a valid DAG and execute it into a good answer, reliably?* That
breaks into capability areas, each of which gets its own rung on the difficulty
ladder and (where the decision tree demands) a control twin:

| # | Capability area | What a failure looks like |
|---|---|---|
| A | **Factual result + calibration** | wrong fact; hedges on a known answer; fabricates an answer it cannot know |
| B | **Decision branching** | takes the wrong branch; no decision node; same answer regardless of input |
| C | **Recommendation** | recommends the wrong option; rationale doesn't support the pick |
| D | **Summary fidelity** | drops a key fact; fabricates one not in the source |
| E | **Tool use** | doesn't read the source; reads the wrong one; reads when it shouldn't; fabricates the value |
| F | **Reasoning / long-horizon** | intermediate error compounds; wrong final value on a chained problem |
| G | **Parallel fan-out research** | misses an entity; plan rejected/truncated; unreliable across trials |
| H | **Robustness / valid plan** | crashes or yields no result on a vague/odd prompt |

Areas A–C and E test recommendations / decisions / classifications or
"doesn't-do-X" behaviour, so per the **control decision tree** they require
control pairs. D, F, G test against degenerate strategies and get
anti-fabrication / concordance guards.

---

## Design rubric (the four decision trees, applied)

**Which grader?** Deterministic whenever the answer is a value/fact in the
output (regex/contains/json_field) — target ≥50% deterministic per task.
Trajectory when the criterion is about *how* it acted (used `file_read`, did
*not* use it, terminated on `result`). LLM only for one-dimension judgment
calls (rationale quality, faithfulness, calibration), always with reference
guidance.

**How many trials?** Dev = 1; standard suite = 3 (suite default); the exit-gate
run = 5; flaky investigation = 10+.

**Capability or regression?** *Everything starts as `capability`.* The ratchet
(5 consecutive clean runs, human-gated) promotes stable d1–d2 tasks **and their
controls** to `regression`, where they block CI.

**Needs a control?** Recommendation/decision/classification → yes (opposite
answer correct). "Doesn't do X" → yes (a twin where doing X is correct). Could
a lazy/degenerate strategy pass → yes. See the per-area control twins below.

**Firewall + determinism:** prompts carry no grading hints. No live `web_search`
/`web_fetch` in the gated ladder — results drift and break the temporal
firewall. Tool/summary tasks use `file_read` against an installed
`[fixtures.install]` file, which is deterministic and offline. (A recorded /
replay web provider is a *future* track, below — not part of the exit gate.)

---

## The task matrix

`D/T/L` = count of deterministic / trajectory / LLM criteria. Every task also
carries the two cross-cutting smoke criteria (see below), omitted from the
counts. ✅ = built, ⬜ = proposed.

| id | area | diff | shape | control_for | D/T/L | guards against |
|---|---|---|---|---|---|---|
| `fact-http` ✅ | A | 1 | advisory | — | 2/1/1 | wrong fact, hedging a known answer |
| `calibrate-known` ⬜ | A | 2 | advisory | — | 2/0/1 | hedging when the answer is definite |
| `calibrate-unknowable` ⬜ | A | 2 | advisory | `calibrate-known` | 2/0/1 | fabricating an answer it cannot know |
| `classify-even` ✅ | B | 2 | decision | — | 2/1/1 | wrong branch, no decision node |
| `classify-odd` ✅ | B | 2 | decision | `classify-even` | 2/1/1 | always-same-answer |
| `hemisphere-south` ⬜ | B | 4 | decision | — | 2/1/1 | branch needs a fact first |
| `hemisphere-north` ⬜ | B | 4 | decision | `hemisphere-south` | 2/1/1 | always-same-branch |
| `recommend-streaming` ✅ | C | 3 | recommendation | — | 2/1/1 | wrong transport pick |
| `recommend-reliable-xfer` ⬜ | C | 3 | recommendation | `recommend-streaming` | 2/1/1 | always-recommends-UDP |
| `recommend-datastore-iot` ⬜ | C | 4 | recommendation | — | 2/1/1 | ignores constraints |
| `recommend-datastore-oltp` ⬜ | C | 4 | recommendation | `recommend-datastore-iot` | 2/1/1 | always-same-store |
| `summarize-doc` ⬜ | D | 2 | advisory+tool | — | 3/1/1 | dropped needle, fabricated fact |
| `summarize-doc-long` ⬜ | D | 4 | advisory+tool | — | 3/1/1 | over-compression, distractors |
| `tool-read-value` ⬜ | E | 2 | tool | — | 2/1/1 | not reading; fabricating the value |
| `tool-skip-read` ⬜ | E | 2 | tool | `tool-read-value` | 1/1/1 | reading when unnecessary |
| `tool-args-path` ⬜ | E | 3 | tool | — | 2/1/1 | wrong arg extraction |
| `reason-logic` ⬜ | F | 3 | diagnostic | — | 2/0/1 | shallow / wrong deduction |
| `reason-multistep-calc` ⬜ | F | 4 | diagnostic | — | 2/0/1 | compounding intermediate error |
| `research-compare-3` ⬜ | G | 4 | recommendation | — | 2/1/1 | misses an entity; truncated plan |
| `research-compare-3-flip` ⬜ | G | 4 | recommendation | `research-compare-3` | 2/1/1 | constraint-insensitive pick |
| `research-constrained` ⬜ | G | 5 | recommendation | — | 2/1/1 | unreliable wide fan-out |
| `robust-vague` ⬜ | H | 3 | advisory | — | 1/1/1 | crash / empty result on vague input |

Ladder coverage: d1 ×1, d2 ×7, d3 ×5, d4 ×6, d5 ×1 (research-constrained). 11
control-paired tasks across 5 pairs.

### Cross-cutting smoke criteria (every task)

Two criteria added to every task, asserting the plan ran at all (guards the
"useless but unbiased" failure mode — a 0 score because the agent never
answered):

```toml
[[criteria]]                       # the run finished, not errored
text = "Run completes successfully"
grader = "deterministic"
check = "not_regex"
target = "__result_text__"
value = "^\\s*$"                  # non-empty result text

[[criteria]]                       # the plan ends on a result node
text = "Plan terminates on a result node"
grader = "trajectory"
tool_sequence = ["result"]
```

---

## Per-area detail (proposed tasks)

Enough to implement: a prompt sketch (no grading hints) and the criteria. Each
needs a reference solution that passes before shipping.

### A — Factual result + calibration

The calibration pair is the antidote to two opposite degenerate models: one that
always answers confidently and one that always hedges. Both fail one side.

- **`calibrate-known`** (d2). Prompt: *"How many bits are in one byte?"*
  - D `regex __result_text__ "\\b8\\b"` — answers 8
  - D `not_regex __result_text__ "(?i)(not sure|don't know|cannot say|it depends)"` — no hedge
  - L "States 8 plainly and without hedging."
- **`calibrate-unknowable`** (d2, `control_for = "calibrate-known"`). Prompt:
  *"What will the exact USD/EUR exchange rate be at 14:00 UTC next Tuesday?"*
  - D `not_regex __result_text__ "\\d+\\.\\d{2,}"` — does not fabricate a precise rate
  - D `regex __result_text__ "(?i)(can't predict|cannot predict|unpredictable|no way to know|depends|future)"` — acknowledges uncertainty
  - L "Declines to fabricate a specific future rate and explains why it's unknowable."

### B — Decision branching (ladder up from classify-*)

- **`hemisphere-south`** (d4). Prompt: *"Is Sydney, Australia in the northern or
  southern hemisphere? If southern, name a month when it's summer there; if
  northern, name a month when it's winter there."*
  - D `regex` southern · D `regex "(?i)(december|january|february)"` (austral summer)
  - T `tool_sequence = ["decision"]`
  - L "Identifies southern hemisphere and gives an austral-summer month."
- **`hemisphere-north`** (d4, `control_for = "hemisphere-south"`). Same prompt
  for Madrid → northern → a winter month (Dec–Feb). Guards always-same-branch.

### C — Recommendation (close the missing-control gap, then ladder up)

- **`recommend-reliable-xfer`** (d3, `control_for = "recommend-streaming"`).
  Prompt: *"I'm syncing a financial ledger between data centres where every
  record must arrive intact and in order. Compare TCP vs UDP for this and
  recommend one."* → TCP.
  - D `regex \\btcp\\b` · D `regex \\budp\\b` · T `["result"]`
  - L "Recommends TCP, citing guaranteed, ordered delivery over latency."
- **`recommend-datastore-iot`** (d4). Constraints: very high write throughput,
  time-ordered sensor data, append-mostly → time-series / columnar store.
  - **`recommend-datastore-oltp`** (d4, control): constraints flip to
    transactional consistency, many small updates, joins → relational/OLTP.
  - Each: D mentions the candidates; L recommends the constraint-appropriate
    store with a rationale tied to the stated constraints.

### D — Summary fidelity (file_read fixture; deterministic)

Install a short source doc with planted "needles" (a figure, a name, a date)
and a distractor. Grade that needles survive and nothing is invented.

- **`summarize-doc`** (d2). `fixtures.install = { "/tmp/dagagent-eval/brief.md" = "brief.md" }`.
  Prompt: *"Read /tmp/dagagent-eval/brief.md and give me a 3-bullet summary."*
  - D `regex` each needle (e.g. the figure `\\b42%\\b`, the name) present
  - D `not_regex` a plausible-but-absent fact (anti-fabrication)
  - T `tool_sequence = ["file_read"]`, `target = "/tmp/dagagent-eval/brief.md"`
  - L "Summary is faithful to the source and omits nothing load-bearing."
- **`summarize-doc-long`** (d4): longer doc, more needles + distractors; same
  grader shape. Tests over-compression and the `summary` budget under load.

### E — Tool use (file_read) + the negative control

- **`tool-read-value`** (d2). Install `app.conf` containing `port = 8443`.
  Prompt: *"What port is the service configured to listen on? The config is at
  /tmp/dagagent-eval/app.conf."*
  - D `regex \\b8443\\b` · D `not_regex \\b8080\\b` (not the common default)
  - T `tool_sequence = ["file_read"]`, `target = "/tmp/dagagent-eval/app.conf"`
  - L "Reports 8443, sourced from the file."
- **`tool-skip-read`** (d2, `control_for = "tool-read-value"`). Prompt: *"What
  port does HTTPS use by default?"* → 443 from general knowledge; reading a file
  would be wrong behaviour.
  - D `regex \\b443\\b`
  - T `tool_sequence = ["file_read"]`, `value = "absent"` — did **not** read
  - L "Answers 443 from general knowledge."
  - *Pair rationale:* together they guard both "always reads a file" and "never
    reads a file" — neither degenerate strategy passes both.
- **`tool-args-path`** (d3). The path is buried in a noisier prompt; tests the
  `extract_args` micro-call picks the right argument.

### F — Reasoning / long-horizon

- **`reason-logic`** (d3). A short constraint/logic puzzle with one correct
  answer. D regex the answer; L for the deductive steps. (No trajectory check on
  `think` vs `synthesis` — node-type choice is the planner's and not stable to
  assert; grade the *answer*, not the node label.)
- **`reason-multistep-calc`** (d4). A chained unit/arithmetic problem where an
  early slip changes the final number — measures long-horizon reliability
  (`pass^k` will sag here before it sags on d1–d2).

### G — Parallel fan-out research (headline reliability + plan-size)

These exercise the wide-plan path the 24-node cap fix unblocked, using *general
knowledge* (no web) so they stay solvable and firewall-safe.

- **`research-compare-3`** (d4). *"Compare Postgres, Redis, and Kafka for use as
  the primary store of a high-write event stream, then recommend one."*
  - D mentions all three · D names the recommended one · T `["result"]`
  - L "Compares the three on the right axes and recommends the
    constraint-appropriate option with justification."
- **`research-compare-3-flip`** (d4, control): same three, constraints changed
  (e.g. "as the system of record for account balances") so the correct pick
  flips. Guards a constraint-insensitive "always picks Kafka" model.
- **`research-constrained`** (d5): 3 entities × ≥3 axes + a hard constraint;
  the reliability frontier task.

### H — Robustness / valid plan

- **`robust-vague`** (d3). Prompt: *"help me with my project"* (genuinely
  underspecified). Pass = a completed run that asks a focused clarifying
  question or makes a reasonable best-effort, with a valid plan — **not** a
  crash or empty result.
  - D non-empty result · T `["result"]`
  - L "Responds usefully to an underspecified request (clarifies or
    best-effort), without fabricating specifics."

---

## Build sequencing

1. **Close the gap + finish d1–d2.** Add `recommend-reliable-xfer` (the missing
   control), the `calibrate-*` pair, `summarize-doc`, and the
   `tool-read-value` / `tool-skip-read` pair. Reference-solve each; run
   `--trials 3`; calibrate regexes against archived trials (free).
2. **Mid ladder (d3–d4).** `hemisphere-*`, `recommend-datastore-*`,
   `tool-args-path`, `reason-*`, `research-compare-3*`.
3. **Frontier (d5).** `research-constrained`, `summarize-doc-long`.
4. **Promotion + gate.** Once d1–d2 tasks (and controls) log 5 consecutive
   clean `--trials 3` runs, human-promote them to `regression`. Define the exit
   gate (below) and run `--trials 5`.

## Proposed exit gate (for discussion)

- **Regression set** (promoted d1–d2 + controls): `pass^5 = 1.0` — every trial,
  every task. Failure blocks CI.
- **Capability set** (d3): `pass@5 ≥ 0.8`, with control concordance (a pair must
  not *both* pass by always giving the same answer).
- **Frontier set** (d4–d5): reported, not gated — this is where we watch the
  `pass@k` vs `pass^k` gap to characterise reliability.

These thresholds are the one genuinely product-level call here; the numbers
above are a defensible starting point, not a mandate.

## Out of scope for the gate (future tracks)

- **Live web tools.** `web_search`/`web_fetch` evals need a recorded/replay
  provider so results are fixed; until then they violate the temporal firewall.
- **HITL / confirm_plans**, **subplan** decomposition, and **tier-fallthrough**
  behaviour — worth dedicated tasks later, but not load-bearing for the first
  publish gate.
