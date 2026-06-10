# Spec: Grassroots marketing

## Purpose

Introduce Glassrail to the right technical audience in a way that earns trust,
feedback, and reputation value. The goal is not mass attention. The goal is to
start high-quality conversations with people who understand why reliable
agentic systems are hard.

Glassrail should position its author as someone who can build serious AI
infrastructure: typed plans, validation, execution semantics, deterministic
model routing, approval gates, telemetry, regression evals, and operational
judgment around model behavior.

## Launch thesis

Most agent demos optimize for magic. Glassrail optimizes for inspection.

Agentic workflows become easier to trust when the plan is explicit, the graph is
validated, each node runs with fresh declared context, model routing is
deterministic, and behavior is measured by repeatable evals. That is the
marketing argument and the engineering argument at the same time.

## Audience

Prioritize smaller, more technical audiences before broad launch channels:

- AI infrastructure engineers.
- Agent framework builders.
- LLM eval and observability practitioners.
- Developer tooling founders and engineers.
- Hiring managers or staff engineers evaluating AI-native systems taste.
- OSS users who are willing to try an early 0.x project and give sharp feedback.

## Non-goals

- Viral launch mechanics.
- Overstating production maturity.
- Picking fights with existing frameworks.
- Broad claims that Glassrail is a universal agent solution.
- Launching before install, docs, and known release blockers are clean.

## Narrative pillars

### 1. Reliability rails for agents

Glassrail is about wrapping probabilistic model behavior with deterministic
system boundaries. The phrase "rails" should mean concrete mechanics:
validation, typed nodes, routing rules, event streams, approval points, and
evals.

### 2. DAG plans over opaque loops

The core contrast is not "framework A is bad". The contrast is inspectable DAG
execution versus opaque agent loops. A DAG plan can be validated, visualized,
replayed, tested, and reviewed before it runs.

### 3. Fresh context per node

Fresh declared context is a strong technical hook. It says Glassrail cares about
context hygiene, traceability, and reducing accidental dependence on irrelevant
conversation history.

### 4. Evals as a release gate

The project should talk about evals as an engineering habit, not a scoreboard.
The message: Glassrail only moves toward release when measured behavior clears
the gate, and remaining misses become named ratchets.

### 5. Model routing economics

Deterministic tier routing makes cost and reliability explicit. This matters to
teams trying to balance local models, cloud fallbacks, latency, and quality.

### 6. Auditable tool use

Approval gates, event streams, and future file-editing controls make Glassrail
relevant to teams that need to trust tool-using agents under operational
pressure.

## Launch prerequisites

- PyPI package is live and installable.
- Website is live and links to GitHub, PyPI, docs, evals, and roadmap.
- README is crisp and current.
- One demo task is reproducible.
- Latest eval gate result is documented.
- Known limitations are written down.
- A short launch post and a deeper technical post are drafted.

## Assets to prepare

### Short launch post

Target length: 150-250 words.

Purpose: announce the project and invite technical feedback.

Required beats:

- Glassrail turns tasks into validated DAG plans.
- Each node runs with fresh declared context.
- Model tier routing is deterministic.
- The project has an eval gate and current results.
- It is early, 0.x, and looking for feedback from agent infrastructure people.

### Technical deep dive

Target length: 1,200-2,000 words.

Working title options:

- "Making agent workflows inspectable with DAG plans"
- "What reliability rails for agents actually mean"
- "From agent loops to validated execution graphs"

Required beats:

- The problem with opaque agent loops.
- Why explicit DAGs help.
- How validation changes failure modes.
- Why fresh context matters.
- How deterministic routing changes cost and reliability.
- What evals caught.
- What remains unsolved.

### Demo

The demo should be boring in the right way: it should show the plan, validation,
node execution, and final answer rather than only a polished output.

Good demo candidates:

- A compare-and-recommend task with multiple named criteria.
- A research summary task that benefits from explicit synthesis.
- A branch task that shows decision behavior.

Avoid demos that require private credentials, fragile web access, or unreleased
file editing tools.

## Channel plan

### Private technical review

Start with 5-10 trusted engineers. Ask for specific critique:

- Does the reliability thesis land?
- Is the README enough to try it?
- Are the evals credible?
- What part feels hand-wavy?
- What would make this useful in a real workflow?

### Personal network post

Use LinkedIn, X, or both depending on where the right reviewers are. The post
should be direct and technical. The ask should be feedback, not stars.

### Targeted communities

Share only where the project is relevant and where self-promotion is allowed.
Good fits may include AI engineering, LLM evals, agent tooling, and developer
infrastructure communities.

Lead with the technical problem and what you want reviewed. Do not drop a link
without context.

### Hacker News

Consider HN after the website, install path, and technical deep dive are
polished. HN will test the claim density and the novelty of the framing. A good
title should be concrete:

- "Show HN: Glassrail - DAG-planned agent workflows with eval-gated releases"
- "Show HN: I built a DAG execution layer for inspectable agent workflows"

### Direct outreach

Send short, specific notes to people working on agent infrastructure, evals,
observability, or model routing. Ask for one piece of feedback. Do not ask them
to adopt it.

## Cadence

### Week 0: Soft launch

- Publish PyPI and website.
- Send to trusted reviewers.
- Fix install and docs friction immediately.

### Week 1: Public announcement

- Post the short launch note.
- Share the website and GitHub repo.
- Track questions and confusion.

### Week 2: Technical deep dive

- Publish the longer post.
- Show the architecture and eval philosophy.
- Invite critique from agent infrastructure builders.

### Week 3: Feedback release

- Ship a small patch release or docs update based on feedback.
- Write a brief "what changed from feedback" note.
- Decide the next public ratchet: result-node preservation, file editing, HITL,
  or TUI workflows.

## Messaging guardrails

Use:

- "early"
- "0.x"
- "reliability infrastructure"
- "inspectable"
- "validated DAG"
- "fresh context"
- "deterministic routing"
- "eval-gated"

Avoid:

- "production-ready" unless the support bar is actually there.
- "autonomous software engineer" as the primary frame.
- "LangChain killer" or similar comparisons.
- "solves hallucination".
- Any claim that eval scores generalize beyond the suites being run.

## Success metrics

Prefer signal quality over volume:

- 5-10 serious technical conversations.
- 2-3 people successfully install and run it.
- 2-3 useful issues, questions, or PRs.
- One strong external critique that changes the roadmap or docs.
- Inbound interest from someone evaluating AI infrastructure skill.

Secondary metrics:

- GitHub stars.
- Repo clones.
- Website visits.
- PyPI downloads.

Do not optimize for secondary metrics at the expense of technical trust.

## Feedback loop

Create a lightweight feedback log after launch:

- Question or objection.
- Source/channel.
- What it implies.
- Action taken.

Common decisions:

- If people do not understand the category, improve homepage positioning.
- If people cannot install it quickly, prioritize package and README fixes.
- If people challenge eval credibility, improve methodology explanation.
- If people ask for file editing and HITL, prioritize the Phase 2 design work.
- If people compare it to existing frameworks, clarify the reliability
  infrastructure thesis without becoming adversarial.

## Acceptance criteria

- Launch assets are drafted before public posting.
- Website and PyPI are live before broad marketing.
- Public claims are accurate and linked to evidence.
- At least five targeted reviewers are asked for feedback.
- Feedback is captured and converted into docs, roadmap, or implementation
  changes.
