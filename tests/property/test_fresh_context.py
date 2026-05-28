"""Property tests for the fresh-context-per-node invariant.

The invariant: ``assemble_context(node, results)`` produces text that
references *only* the upstream nodes listed in ``node.context_needed``.
Content from any other completed node in ``results`` must not leak in.

These tests pair each node id with a unique sentinel string in its
output. After assembly we assert no sentinel for an out-of-context node
appears in the result. Hypothesis explores random graph sizes, random
subsets-as-context, and the full set of upstream node statuses.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dagagent.core import Node, NodeResult, NodeStatus, NodeType
from dagagent.executor import assemble_context


def _sentinel(node_id: int) -> str:
    return f"__SENTINEL_NODE_{node_id}__"


_STATUSES = [
    NodeStatus.COMPLETED,
    NodeStatus.SKIPPED,
    NodeStatus.EMPTY,
    NodeStatus.FAILED,
]


@st.composite
def _scenario(
    draw: st.DrawFn,
) -> tuple[list[int], list[int], dict[int, NodeResult]]:
    """Build (all_ids, context_needed, results) for one property case."""
    all_ids = draw(
        st.lists(
            st.integers(min_value=1, max_value=999),
            min_size=1,
            max_size=15,
            unique=True,
        )
    )

    # context_needed is a (possibly empty) ordered subset of all_ids,
    # with a small chance of including an id that has no result at all.
    needed_size = draw(st.integers(min_value=0, max_value=len(all_ids)))
    permuted = draw(st.permutations(all_ids))
    needed: list[int] = permuted[:needed_size]
    if draw(st.booleans()):
        # Inject one id that won't appear in results — exercises the
        # "treated as skipped" branch in the assembler.
        ghost = draw(st.integers(min_value=1000, max_value=2000))
        needed.append(ghost)

    results: dict[int, NodeResult] = {}
    for nid in all_ids:
        status = draw(st.sampled_from(_STATUSES))
        sentinel = _sentinel(nid)
        if status is NodeStatus.FAILED:
            results[nid] = NodeResult(node_id=nid, status=status, error=sentinel)
        else:
            results[nid] = NodeResult(node_id=nid, status=status, output=sentinel)

    return all_ids, needed, results


def _make_node(context_needed: list[int]) -> Node:
    return Node(
        id=0,
        type=NodeType.SYNTHESIS,
        description="probe",
        context_needed=context_needed,
    )


@given(_scenario())
def test_no_out_of_context_sentinel_leaks(
    scenario: tuple[list[int], list[int], dict[int, NodeResult]],
) -> None:
    """No content from a non-requested upstream node appears in the output."""
    all_ids, needed, results = scenario
    text = assemble_context(_make_node(needed), results)

    needed_set = set(needed)
    for nid in all_ids:
        if nid not in needed_set:
            assert _sentinel(nid) not in text, (
                f"Out-of-context node {nid} leaked into assembled prompt"
            )


@given(_scenario())
def test_every_completed_dep_is_present(
    scenario: tuple[list[int], list[int], dict[int, NodeResult]],
) -> None:
    """If a needed dep has a COMPLETED result, its content reaches the prompt."""
    _all_ids, needed, results = scenario
    text = assemble_context(_make_node(needed), results)

    for nid in needed:
        result = results.get(nid)
        if result is not None and result.status is NodeStatus.COMPLETED:
            assert _sentinel(nid) in text, (
                f"Completed needed dep {nid} missing from assembled prompt"
            )


@given(_scenario())
def test_empty_context_needed_yields_empty_string(
    scenario: tuple[list[int], list[int], dict[int, NodeResult]],
) -> None:
    """A node that declares no upstream needs sees no upstream content."""
    _all_ids, _needed, results = scenario
    text = assemble_context(_make_node([]), results)
    assert text == ""


@given(_scenario())
def test_ordering_follows_context_needed(
    scenario: tuple[list[int], list[int], dict[int, NodeResult]],
) -> None:
    """Markers appear in the order declared by ``context_needed``."""
    _all_ids, needed, results = scenario
    text = assemble_context(_make_node(needed), results)

    positions: list[int] = []
    for nid in needed:
        # `Node {nid} output` is the exact marker prefix the assembler
        # emits for both COMPLETED outputs and substitution notices.
        # The trailing space after the id keeps "Node 1" from matching
        # "Node 10".
        marker_idx = text.find(f"Node {nid} output")
        assert marker_idx >= 0, f"Needed dep {nid} produced no marker"
        positions.append(marker_idx)

    assert positions == sorted(positions), "Context order diverged from context_needed order"
