"""Per-node context assembly.

Honours the fresh-context-per-node invariant: a node sees only the upstream
outputs it declared in ``context_needed`` — never the global execution state,
never sibling node outputs that weren't requested.
"""

from __future__ import annotations

import json

from dagagent.core import Node, NodeResult, NodeStatus


def _truncate_middle(text: str, max_chars: int) -> str:
    """Preserve the start and end of long outputs; drop the middle.

    Middle truncation works better than tail truncation for structured data
    — JSON arrays, paginated lists, etc. — where the tail often carries
    just as much signal as the head.
    """
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n... [truncated] ...\n" + text[-half:]


def assemble_context(
    node: Node,
    results: dict[int, NodeResult],
    *,
    max_chars_per_dep: int = 4000,
) -> str:
    """Build the prompt context for one node.

    Skipped, empty, or failed upstream nodes get a substitution notice
    rather than raising, so a branch-skip elsewhere in the graph doesn't
    cascade into a hard execution error.
    """
    if not node.context_needed:
        return ""

    parts: list[str] = []
    for dep_id in node.context_needed:
        result = results.get(dep_id)
        if result is None or result.status is NodeStatus.SKIPPED:
            parts.append(f"[Node {dep_id} output: not available (node was skipped)]")
        elif result.status is NodeStatus.EMPTY:
            parts.append(f"[Node {dep_id} output: empty — tool returned no results]")
        elif result.status is NodeStatus.FAILED:
            parts.append(f"[Node {dep_id} output: FAILED — {result.error}]")
        else:
            raw = result.output if isinstance(result.output, str) else json.dumps(result.output)
            truncated = _truncate_middle(raw, max_chars_per_dep)
            parts.append(f"[Node {dep_id} output]:\n{truncated}")

    return "\n\n".join(parts)
