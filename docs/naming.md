# Naming and Positioning Spec

## Recommendation

Use **Glassrail** as the leading candidate for the project name.

Glassrail is the strongest public brand from the current shortlist because it
combines two ideas the project needs to own: inspectability and control. The
name should not be framed as transparency alone. The stronger story is that
agentic systems need execution paths that are visible, constrained, and
load-bearing.

The name should be styled as **Glassrail**, with one capital letter at the
start. Avoid **GlassRail**. The internal capital makes the name feel more like a
framework class, SaaS feature, or Rails-adjacent developer tool. `glassrail` is
the preferred lowercase form for package, binary, repository, and config names.

## One-line positioning

**Glassrail is transparent execution infrastructure for agentic systems.**

Alternate taglines:

- Visible rails for reliable agent work.
- Make agentic work visible enough to trust and structured enough to run.
- Turn AI work from a hidden loop into a visible execution path.
- Typed plans, validated execution, and traces for AI work.
- Transparent control infrastructure for AI-assisted work.

## Marketing frame

Most agent projects emphasize autonomy: the model can think, act, observe, and
loop until it gets somewhere useful. That can look impressive in a demo, but it
is not enough for systems that need to run under real operational pressure.
When the model's work cannot be inspected, replayed, measured, or bounded, the
team is left trusting a transcript instead of trusting a system.

Glassrail takes the opposite posture. It treats agentic work as infrastructure:
the model proposes a typed plan, the validator checks the graph, the executor
runs each node with fresh declared context, tier routing follows deterministic
rules, approvals happen at explicit gates, and the whole run leaves telemetry
and eval artifacts behind. The project sits at the messy boundary between
probabilistic model behavior and deterministic production systems, where the
most interesting reliability work in AI infrastructure is happening.

The career signal is also part of the product signal. Glassrail should show
that its builder is not merely experimenting with agents, but knows how to wrap
unreliable model outputs in production-shaped semantics: schemas, validation,
state, routing, approval gates, observability, and regression evals. The point
is not "I can build a chatbot." The point is "I can make AI-assisted work
auditable, testable, cheaper, safer, and harder to fake."

In that frame, Glassrail becomes a name for the infrastructure layer that lets
teams trust agentic systems. Glass is the inspectability story: no hidden loop,
no black box, no unreviewable transcript theater. Rail is the execution story:
the system lays down typed paths, constraints, routing, and gates before work
runs. Together, the name says that agent work should move through visible
structure.

## Why Glassrail works

### It is more than transparency

Transparency by itself can sound passive: a dashboard, a trace viewer, a window
into a system that still behaves however it wants. Glassrail is stronger because
it pairs visibility with control. The execution path is not merely observable;
it is shaped by a validated graph, explicit dependencies, fresh context, tier
routing, and approval gates.

### It bridges elegance and infrastructure

The name is lighter and more brandable than Checkrail, but it still has an
infrastructure spine. "Glass" gives it clarity and memorability. "Rail" gives it
motion, constraints, routing, and operational seriousness. That combination
fits developer infrastructure better than a purely poetic or purely mechanical
name.

### It supports the product architecture

Glassrail maps naturally to the system's actual primitives:

- Validated DAGs are the rails.
- Fresh context per node keeps each segment inspectable.
- Tier routing is switchyard logic.
- Events and telemetry are the glass around the run.
- Evals prove the rails keep holding over time.
- HITL gates are controlled crossings.

### It can carry a visual identity

The brand can use route diagrams, transparent layers, rail lines, switches,
signal marks, glass panels, inspection windows, and run traces. That visual
system is richer than generic graph-node imagery and easier to extend across a
README, docs site, CLI output, TUI, and future product surfaces.

## Core message pillars

### Visible

Agent work should not disappear into a hidden loop. A Glassrail run becomes a
visible graph of typed steps, node outputs, branch decisions, model calls, and
events.

### Structured

The model does not get unlimited discretion over execution. Plans are validated
before they run, nodes declare the context they need, and control flow is
explicit in the graph.

### Measurable

Reliability is not a vibe. The project has an eval framework, pass@k versus
pass^k reporting, regression gates, and baseline tracking so behavior can be
ratcheted over time.

### Repeatable

Plans are documents. Routing is deterministic. The system is built to make runs
easier to inspect, compare, and replay.

### Practical

The project is not just a philosophy of agent reliability. It is a working
runtime with CLI, REST, streaming events, ACP, TUI, state storage, telemetry,
and eval harnesses.

## Comparison with the other finalists

### Checkrail

Checkrail is the sharper engineering metaphor for keeping execution aligned
through risky branching paths. It is precise and serious, but narrower. It
leans more toward safety mechanism than full public brand, and its namespace
surface is less clean.

### Proofmark

Proofmark is strong for evals, provenance, and auditability. It suggests that
every output carries evidence of how it was produced. The risk is that it
underplays planning, routing, and execution; it may sound like a verification
product rather than a runtime.

### Waymark

Waymark is friendly and easy to understand. It maps well to nodes as visible
markers along a route. The risk is softness: it suggests guidance more than
trust under operational pressure, and the namespace is crowded.

### Chartwright

Chartwright has a nice craft story: the system makes the chart before the work
runs. It is distinctive and availability-friendly, but a little whimsical. It
also risks being read as data visualization or dashboards unless the surrounding
copy makes "chart" mean an executable plan.

## Naming rules

- Use **Glassrail** in prose and headings.
- Use `glassrail` for package, binary, repository, and config identifiers.
- Do not use **GlassRail**.
- Do not frame the name as transparency alone. Pair visibility with execution
  control.
- Explain the metaphor once, then let the product language focus on plans,
  traces, validation, routing, gates, and evals.
- Keep the voice calm, precise, and systems-oriented. Avoid hype around
  autonomy, magic, or artificial generality.

## First-pass homepage copy

**Glassrail**

Transparent execution infrastructure for agentic systems.

Glassrail turns AI-assisted work into typed, validated execution graphs. The
planner proposes a DAG, the validator checks the structure, the executor runs
each node with fresh declared context, and the run leaves behind events, traces,
and eval artifacts that can be inspected later.

It is built for the boundary where probabilistic models meet deterministic
systems. Instead of trusting a hidden agent loop, teams get plans, gates,
routing, telemetry, and regression checks around model behavior.

## Decision criteria before launch

- The name should survive saying out loud in a technical interview, design
  review, README, conference hallway, and GitHub repo.
- The name should make the project feel like infrastructure, not a demo.
- The name should support the line: "Glassrail makes agentic work visible enough
  to trust and structured enough to run."
- The name should remain credible if the system grows beyond DAG planning into
  broader agent runtime, tool safety, memory, scheduling, and workflow
  governance.
- Trademark, package, GitHub, and domain availability must be checked before a
  final public launch.
