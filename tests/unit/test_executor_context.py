"""Tests for the per-node context assembler."""

from __future__ import annotations

from glassrail.core import Node, NodeResult, NodeStatus, NodeType
from glassrail.executor import assemble_context


def _node(*, ctx: list[int]) -> Node:
    return Node(id=10, type=NodeType.SYNTHESIS, description="x", context_needed=ctx)


def test_no_context_needed_returns_empty() -> None:
    node = _node(ctx=[])
    assert assemble_context(node, {}) == ""


def test_completed_dep_string_output() -> None:
    node = _node(ctx=[1])
    results = {1: NodeResult(node_id=1, status=NodeStatus.COMPLETED, output="hi there")}
    text = assemble_context(node, results)
    assert "[Node 1 output]" in text
    assert "hi there" in text


def test_completed_dep_dict_output_serialised() -> None:
    node = _node(ctx=[1])
    results = {1: NodeResult(node_id=1, status=NodeStatus.COMPLETED, output={"k": "v"})}
    text = assemble_context(node, results)
    assert "[Node 1 output]" in text
    assert '"k": "v"' in text


def test_skipped_dep_substitutes() -> None:
    node = _node(ctx=[2])
    results = {2: NodeResult(node_id=2, status=NodeStatus.SKIPPED)}
    text = assemble_context(node, results)
    assert "not available" in text


def test_empty_dep_substitutes() -> None:
    node = _node(ctx=[3])
    results = {3: NodeResult(node_id=3, status=NodeStatus.EMPTY, output={})}
    text = assemble_context(node, results)
    assert "empty" in text


def test_failed_dep_substitutes_with_error() -> None:
    node = _node(ctx=[4])
    results = {4: NodeResult(node_id=4, status=NodeStatus.FAILED, error="boom")}
    text = assemble_context(node, results)
    assert "FAILED" in text
    assert "boom" in text


def test_missing_dep_treated_as_skipped() -> None:
    node = _node(ctx=[99])
    text = assemble_context(node, {})
    assert "not available" in text


def test_multiple_deps_joined() -> None:
    node = _node(ctx=[1, 2])
    results = {
        1: NodeResult(node_id=1, status=NodeStatus.COMPLETED, output="a"),
        2: NodeResult(node_id=2, status=NodeStatus.COMPLETED, output="b"),
    }
    text = assemble_context(node, results)
    assert "Node 1" in text
    assert "Node 2" in text


def test_long_output_middle_truncated() -> None:
    node = _node(ctx=[1])
    long = "X" * 10_000
    results = {1: NodeResult(node_id=1, status=NodeStatus.COMPLETED, output=long)}
    text = assemble_context(node, results, max_chars_per_dep=200)
    assert "truncated" in text
    assert len(text) < 1000


def _dep_node(nid: int, desc: str, *, ntype: NodeType = NodeType.RESULT) -> Node:
    return Node(id=nid, type=ntype, description=desc, context_needed=[10])


def test_dependent_nodes_appended() -> None:
    node = _node(ctx=[])
    dep = _dep_node(20, "Summarise findings")
    text = assemble_context(node, {}, dependent_nodes=[dep])
    assert "Your output will be consumed by" in text
    assert "Node 20" in text
    assert "Summarise findings" in text


def test_dependent_nodes_include_type() -> None:
    node = _node(ctx=[])
    dep = _dep_node(21, "Final answer", ntype=NodeType.RESULT)
    text = assemble_context(node, {}, dependent_nodes=[dep])
    assert "result" in text


def test_no_dependent_nodes_none() -> None:
    node = _node(ctx=[])
    text = assemble_context(node, {}, dependent_nodes=None)
    assert "consumed by" not in text


def test_no_dependent_nodes_default() -> None:
    node = _node(ctx=[])
    text = assemble_context(node, {})
    assert "consumed by" not in text


def test_dependent_nodes_combined_with_upstream() -> None:
    node = _node(ctx=[1])
    results = {1: NodeResult(node_id=1, status=NodeStatus.COMPLETED, output="data")}
    dep = _dep_node(20, "Write report")
    text = assemble_context(node, results, dependent_nodes=[dep])
    assert "Node 1 output" in text
    assert "Write report" in text


def test_no_context_needed_with_dependents_not_empty() -> None:
    node = _node(ctx=[])
    dep = _dep_node(20, "consume me")
    text = assemble_context(node, {}, dependent_nodes=[dep])
    assert text != ""


def test_multiple_dependents_all_listed() -> None:
    node = _node(ctx=[])
    deps = [_dep_node(20, "Write report"), _dep_node(21, "Send summary")]
    text = assemble_context(node, {}, dependent_nodes=deps)
    assert "Node 20" in text
    assert "Write report" in text
    assert "Node 21" in text
    assert "Send summary" in text
