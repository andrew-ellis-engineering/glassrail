# Spec: Configurable tier routing table

Status: Implemented (2026-06-19)
Priority: P1 — small (about a day). Prerequisite for the Phase 2.5
`glassrail routing recompute` tier-ROI selector, which needs a routing surface
to write into.
Depends on: nothing.

## Purpose

"Deterministic tier routing" is a headline feature, but the routing *policy*
is three hardcoded lines in `Executor._select_tier`
(`src/glassrail/executor/executor.py`): `decision`/`tool` → tier 0, `think` or
`reasoning_required` → tier 2, everything else → tier 0, `forced_tier`
overrides. Tiers 1 and 3 are reachable only via failure fallthrough. Operators
cannot express "summaries go to tier 1" without forking the code. This spec
makes the table configuration, with defaults that reproduce current behaviour
exactly.

## Design

### Settings

New nested model in `src/glassrail/config/settings.py`, exposed as
`settings.routing` (`[routing]` in `config.toml`, `GLASSRAIL_ROUTING__*` env):

```python
class RoutingConfig(BaseModel):
    decision: int = 0
    tool: int = 0          # tier used by tool-adjacent LLM micro-calls (extract_args, shape_check)
    synthesis: int = 0
    think: int = 2
    summary: int = 0
    result: int = 0
    reasoning_required: int = 2   # floor applied when a node sets reasoning_required
```

A `model_validator` rejects any value outside `0..3` (the tier count is fixed
at four in `Settings`; if that ever becomes dynamic, validate against
`len(tiers)` instead — leave a comment saying so).

`subplan` has no field: a subplan node makes no LLM call of its own; its inner
nodes route individually.

### Executor

`_select_tier(node)` becomes a pure lookup:

1. `base = getattr(settings.routing, node.type.value)` for the six LLM-bearing
   types (`tool` covers the extract-args/shape-check micro-calls that
   currently inherit the tool node's tier — verify how those calls pick their
   tier today and route them through the same value).
2. If `node.reasoning_required`: `base = max(base, settings.routing.reasoning_required)`.
3. `node.forced_tier` overrides everything (the validator already range-checks
   it, including inside subplans — unchanged).

The defaults above make this a behavioural no-op; the existing tier-selection
unit tests in `tests/unit/test_executor.py` must pass unmodified, which is the
proof.

## Tests

- Existing tier tests pass unmodified (no-op proof).
- New: `Settings(routing=RoutingConfig(summary=1))` routes a summary node to
  tier 1; `reasoning_required` on a synthesis node with `routing.synthesis=0`
  routes to `routing.reasoning_required`'s tier; `forced_tier=3` still wins.
- `tests/unit/test_config_settings.py`: env override
  `GLASSRAIL_ROUTING__THINK=1` parses; `routing.think = 7` raises a validation
  error.
- harness-mechanics already pins the defaults (`think-tier2`,
  `synthesis-tier0`, `decision-tier0`, `reasoning-required-tier2`,
  `forced-tier-override`) — they must stay green untouched.

## Docs

README configuration section gains a short "Tier routing" subsection: the
table of fields, defaults, and one example (`[routing] summary = 1`). Note
that fallthrough on provider unavailability is unchanged and orthogonal.

## Non-goals

- Per-task or per-request routing overrides (the plan-level `forced_tier`
  already exists).
- Any automated model selection — that is the Phase 2.5 tier-ROI selector
  (external vault spec), which should ultimately *write* these fields (or a
  `routing_table.json` consumed into them); note this linkage in the roadmap
  entry when implementing.
- Changing planner tier selection (`planner_min_tier` stands).

## Acceptance criteria

- Full check sweep green; harness-mechanics suite green untouched.
- README updated in the same change; CHANGELOG entry added.
- `GLASSRAIL_ROUTING__SUMMARY=1 uv run glassrail run "<task>" --json` routes a
  summary node to tier 1 (observable via the trajectory's `tier_used`).
