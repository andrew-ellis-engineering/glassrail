# Spec: Node Contracts and Context Flow

Status: Part 1 implemented (upstream context awareness); Part 2 planned.
Priority: Phase 2 prerequisite for file editing and `foreach`.
Depends on: nothing for registry metadata; runtime references land with the
first tool-to-tool chain.

---

## Problem

Nodes in a plan don't know what their consumers need from them, and there's no mechanism to verify that a producing node's output matches what a consuming node expects. This shows up as two distinct failure modes with different severities and different fixes.

---

## Two failure modes — keep them separate

### Failure Mode A: Tool→tool key mismatch (real, concrete, enforceable)

A tool node returns `{"path": ..., "content": ...}` but a downstream tool
node's `args_template` references `{{node_3.contents}}`. Without a declared
output schema, that mismatch cannot be rejected before execution.

### Failure Mode B: Synthesis underserves a think node (real, but not enforceable by types)

A synthesis node summarizes "research findings" without knowing that a downstream think node needs specifically the cost trade-offs. The synthesis produces a valid but incomplete summary. **Types cannot fix this** — the synthesis output is prose, and `produces: {analysis: str}` is a vacuous declaration. This is a **prompt-quality problem**, fixed by runtime context injection (description injection), not a schema.

The confusion in the original idea is that it described Failure Mode B but reached for a solution (typed contracts) that only works on Mode A. Keep the fixes separate.

---

## What we're building

### Part 1: Description injection (implemented)

**What it does:** When assembling a node's context, include the `description` fields of its direct downstream dependents (the nodes that declare `context_needed` pointing at this node). A synthesis node sees: "This output will be used by: [think node: 'reason about cost trade-offs between the three options']." This is a prompting improvement that directly answers the original question.

**What it does NOT do:** It provides no structural guarantee. The synthesis node is encouraged, not compelled. That's fine — Failure Mode B is fundamentally not a structural problem.

**Implementation:** One change in `executor/context.py` (or wherever `_build_node_context` lives). Add a helper that walks the plan to find direct consumers of the current node, collects their `description` fields, and appends them to the context block under a heading like "This output will be used by:".

**Files:** `src/glassrail/executor/` (context assembly), possibly `src/glassrail/planner/` (if consumer context should also inform the plan-generation prompt).

---

### Part 2: Tool registry output schemas and references

**What it does:** Let tools declare their output shape at registration time. The validator checks `args_template` references against the producing tool's registered schema. Catches Failure Mode A (tool→tool key mismatches) at plan-validation time, with no change to the plan format and no burden on the LLM planner.

**Key principle:** Contracts belong on tools (static, author-supplied), not on nodes (dynamic, LLM-guessed). Tool authors know what their tool returns; the LLM planner does not.

**Schema shape (in `@harness.tool`):**

```python
@harness.tool(
    name="web_search",
    description="...",
    parameters={...},              # existing: input schema
    output_schema={                # new: output shape declaration
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "snippet": {"type": "string"},
                    }
                }
            }
        }
    }
)
```

**Reference syntax (v1):** an `args_template` value may be an entire reference
string of the form `{{node_<id>.<key>}}`, for example
`{{node_3.path}}`. V1 supports one top-level object key only: no interpolation,
array indexing, JSONPath, or references embedded inside a larger string.
Literal strings remain literal.

**Validator checks:** recursively inspect `args_template` values. For each
reference, require that the source id appears in `context_needed`, resolves to
a tool node, has a registered object `output_schema`, and declares the named
top-level property. When both producer output and consumer input declare JSON
Schema `type`, reject incompatible types. Unknown/omitted output schemas are
not guessed: reject references to them with an actionable registration error.

**Runtime resolution:** immediately before tool approval, replace each valid
reference with the corresponding key from the completed source `NodeResult`.
Missing, failed, skipped, or non-object outputs fail the consumer node closed.
The resolved arguments, not template strings, appear in approval payloads and
`NodeResult.args_used`.

**Planner visibility:** include the author-supplied output schema as the
`x_output_schema` extension in the tool schema sent to the planner. The planner
does not invent contracts; it can only reference paths declared by tool authors.

**Files:**
- `src/glassrail/harness/registry.py` — add `output_schema`, lookup, and `x_output_schema`
- `src/glassrail/harness/builtin.py` — add output schemas to existing built-in tools (`file_read`, `calendar_get`, `memory_search`, and the new file editing tools)
- `src/glassrail/validator/` — parse and validate supported references
- `src/glassrail/executor/` — resolve validated references before approval

**When to build it:** registry metadata may land as its own focused PR. Reference
validation/resolution lands with the first real tool-to-tool chain so it has a
concrete consumer and end-to-end coverage.

---

## What we're NOT building

**Planner-emitted per-node typed contracts (`produces`/`consumes` fields on `Node`).**

This would require the LLM planner to emit a globally consistent output/input schema across all nodes in one shot — a cross-reference bookkeeping task LLMs are bad at. The result would be:
- Plans failing validation on trivially-wrong key names (`cost_analysis` vs `costs`) when the reasoning is correct.
- Vacuous schemas on LLM nodes (synthesis/think/result emit prose; their `produces` is unenforceable).
- Double the per-node field count in the plan JSON.
- Every future plugin tool needing a plan-level schema in addition to the registry schema.

The enforceable part of this idea (tool→tool key checking) is fully covered by registry schemas without touching the plan format. The prose-node part is covered by description injection. There is no remaining use case that justifies the planner burden.

**Decision: planner-emitted per-node contracts are deferred to Phase 4 at earliest, and may never be built.** Revisit only if eval data shows plan-correctness failures in categories that neither description injection nor registry schemas cover.

---

## Interaction with `foreach`

When the `foreach` node type ships (see Foreach spec), a runtime `foreach_source` needs to reference an upstream node whose output is a list. This is a tool-to-node shape dependency: the planner must know that a producing tool returns a list before using it as a `foreach` source. Registry-level output schemas make that checkable. A `foreach` node itself emits its ordered collected results; downstream synthesis, when needed, remains an explicit node. The two specs are designed to land together in Phase 2.

---

## Summary of decisions

| Question | Decision |
|---|---|
| Fix for synthesis-underserves-think | Description injection (prompt quality, not types) |
| Fix for tool→tool key mismatch | Registry output schemas (static, author-supplied) |
| Planner-emitted per-node typed contracts | Deferred indefinitely |
| Where contracts live | On tools (known, static) not on nodes (LLM-guessed) |
| Phase 1 deliverable | Description injection (implemented) |
| Phase 2 deliverable | Registry output schemas and constrained references |
| Gate for registry schemas | When real tool→tool chains exist (Phase 2 file editing) |
