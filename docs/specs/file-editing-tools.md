# Spec: File Editing Tools

Status: Planned; implementation intentionally deferred until the diff-preview and
TUI file-review surfaces are specified together.
Priority: Phase 2 capability layer.
Depends on: [Node contracts and context flow](node-contracts.md), the existing
filesystem confinement and risk-derived approval defaults, and a file-viewer
approval surface.

---

## Problem

The agent has `file_read` but no general write capability. The tool harness
already has a `write` risk tier, risk-derived approval defaults, per-tool HITL
policies, filesystem root confinement, and ACP permission requests. The missing
work is the edit family plus a review surface that lets a person approve the
rendered change rather than opaque arguments.

---

## Locked decisions

1. **Primary surface is `file_edit(path, old_str, new_str)` with exact-once match semantics.** Not unified diffs (LLMs produce invalid `@@` headers), not line numbers (they drift and are especially fragile under the fresh-context model). Exact-string replacement self-anchors: the model proves it read the file by quoting it back, and the match contract fails closed rather than silently corrupting.

2. **Exact-once match is non-negotiable.** Zero matches → error "not found." Multiple matches → error "ambiguous, add more context." This is the single most important safety property of the tool. Do not weaken it.

3. **Ship a small family, not one tool.** Three tools with distinct risk profiles, so per-tool HITL policies are meaningful:
   - `file_edit(path, old_str, new_str)` — surgical replacement, primary tool
   - `file_create(path, content)` — new file only, fails if exists
   - `file_write(path, content)` — full overwrite, must exist; discouraged, for small files

4. **Path-root confinement is required before shipping.** Reuse the existing
   `tools.fs_roots` configuration and `harness.pathguard.ensure_within_roots`.
   Unlike the backward-compatible read path, write tools are default-deny: they
   require at least one configured root and reject an empty/unset root list.

5. **Write tools default to `ask` HITL policy.** This is already enforced by
   the executor's risk-derived defaults (`write`/`execute` → `ask`). Explicit
   per-tool overrides continue to win; the edit tools must register as `write`.

6. **Single-edit per tool call — no batching.** Multiple edits to one file = multiple `file_edit` nodes. This is more auditable, lets each edit be approved or replanned independently, and fits the DAG model. Do not import agentic-loop batch-edit ergonomics.

7. **Specialized file-writing integrations reuse this implementation rather than creating parallel write paths.** Build the general file-editing engine first; any integration-specific wrapper must use the same root confinement, approval, preview, and exact-match checks.

---

## Tool specifications

### `file_edit(path: str, old_str: str, new_str: str) -> dict`

- Read the file at `path` (must exist; reject if not in `fs_roots`).
- Search for `old_str`. Must match exactly once (see locked decision 2).
- Replace the first (and only) occurrence with `new_str`.
- Write the result back, UTF-8, preserving trailing newline, no implicit normalisation.
- Return: `{"path": ..., "diff": "<unified diff of the change>"}` — always include the rendered diff in the return value so downstream nodes and the audit log see the change, even though the input was string-based.
- No-op detection: if `old_str == new_str` or the resulting content is byte-identical, return a no-op result without touching mtime.
- Risk: `write`.

### `file_create(path: str, content: str) -> dict`

- Fail if the file already exists (use `file_write` for overwrites).
- Create parent directories if needed.
- Write `content` UTF-8.
- Return: `{"path": ..., "created": true}`.
- Risk: `write`.

### `file_write(path: str, content: str) -> dict`

- File must exist (use `file_create` for new files).
- Full overwrite. Discourage in planner cookbook — use only for small files / total rewrites.
- Return: `{"path": ..., "bytes_written": N}`.
- Risk: `write`.

---

## Safety guardrails

### 1. Path-root confinement (existing primitive; required by the tools)

```python
# Existing setting
fs_roots: list[Path] | None = None
# Write tools interpret None/empty as default-deny and require explicit roots.
```

Validation logic (in the tool implementations or a shared `_check_path` helper):
```python
def _check_write_path(path: str, roots: list[Path] | None) -> Path:
    if not roots:
        raise ToolExecutionError("Write tools require tools.fs_roots")
    return ensure_within_roots(path, roots)
```

Apply to all three write tools. `file_read` already uses the shared path guard.

### 2. Exact-once match (see locked decisions — this IS a safety property)

### 3. Git-repo guard (configurable, on by default for write tools)

Add `tools.require_git_repo: bool = True`. When enabled, `file_edit`/`file_create`/`file_write` fail if the target path is not within a git working tree. This makes version-control history the available recovery mechanism without building a custom backup system. Make it configurable for managed directories that are not git working trees.

### 4. Preserve risk-derived approval defaults

The executor already derives defaults from tool risk: `read`/`network` →
`allow`, `write`/`execute` → `ask`. Keep direct coverage that every file-editing
tool registers as `write` and that explicit policy overrides still win.

### 5. Diff-in-approval payload

The HITL approval prompt (and the ACP `session/request_permission` event) must
carry the **rendered diff**, not just raw args. Add an optional preview callback
to tool registration. After arguments are resolved but before approval, the
executor invokes that callback without mutating the filesystem and places the
result in `ToolApprovalRequest.preview`. The execution callback must recompute
and revalidate the exact-once match after approval so a stale preview fails
closed. The Rust TUI file viewer renders this preview before accepting approval.

---

## Read-after-write ordering within a plan

Two `file_edit` nodes targeting the same file must be serialised — the second edit's `old_str` may target pre-first-edit content if they run concurrently. The fresh-context-per-node model means the second node may not have seen the first edit's output.

**Approach (two-layer):**
1. *Planner guidance (primary):* same-file edits must be chained via `context_needed` dependencies. Add this to the cookbook and planner system prompt.
2. *Executor guard (safety net):* if two `file_edit` nodes targeting the same resolved path are scheduled to run concurrently (independent in the DAG), serialise them on that path. A per-path write lock in the executor covers this without requiring the planner to model it perfectly.

---

## How the DAG model helps

The natural decomposition `file_read → think/synthesis (decide the change) → file_edit` maps onto three nodes, each at the right tier:
- `file_read` — free, local
- `think` or `synthesis` — reason about what needs changing, top-tier model
- `file_edit` — mechanical tool call, no model needed

Each node is auditable and independently approvable or replannable. Re-executing a write node is not generally independent: filesystem state may have changed, so its exact-match precondition must be checked again. The plan *is* the change set. Contrast with an agentic loop where "read the file, reason, emit edit" is one opaque context: the DAG model gives plan-level approval of the declared paths and per-edit approval of each rendered diff.

The exact-once match rule turns the fresh-context model from a liability into a safety property: if the file changed between when node A read it and when node B edits it, the match fails closed rather than silently corrupting.

---

## Files to touch

- `src/glassrail/harness/integrations/files.py` — add `file_edit`, `file_create`, `file_write`, shared path/git checks, and preview callbacks
- `src/glassrail/harness/registry.py` — register optional preview callbacks and output schemas
- `src/glassrail/config/settings.py` — add `tools.require_git_repo`; reuse `tools.fs_roots`
- `src/glassrail/executor/executor.py` — request previews before approval and add per-path write serialisation
- `src/glassrail/executor/tool_approval.py` — add `preview: str | None` to approval payload
- `src/glassrail/gateways/acp/server.py` — render diff in `session/request_permission` event
- `src/glassrail/planner/` — add file-editing cookbook recipes, same-file-ordering guidance
- `tests/unit/` and `tests/integration/` — exact-once, path, git, preview, approval, stale-preview, and same-path concurrency coverage

Model-quality editing evals remain deferred until the tool safety and TUI
review surfaces are complete; deterministic tool mechanics belong in pytest.
