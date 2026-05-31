"""PlanValidator — every structural check a plan must pass before execution.

Raises :class:`PlanValidationError` on any failure. On success, mutates
``plan.sorted_node_ids`` to a topologically sorted list of node ids that
the executor walks in order.
"""

from __future__ import annotations

from collections import defaultdict

from dagagent.config import Settings
from dagagent.core import Node, NodeType, Plan, PlanValidationError
from dagagent.harness import ToolHarness


class PlanValidator:
    """Validates a plan's structure against settings and harness state."""

    def __init__(self, *, harness: ToolHarness, settings: Settings) -> None:
        self._harness = harness
        self._settings = settings

    def validate(self, plan: Plan) -> list[int]:
        """Run every check; populate and return ``plan.sorted_node_ids``."""
        self._check_node_limit(plan, is_subplan=False)
        self._check_tool_names(plan)
        self._check_forced_tiers(plan)
        sorted_ids = self._topological_sort(plan)
        self._check_decision_nesting(plan)
        self._check_branch_references(plan)
        self._check_subplans(plan)
        plan.sorted_node_ids = sorted_ids
        return sorted_ids

    # ── Individual checks ─────────────────────────────────────────────────

    def _check_node_limit(self, plan: Plan, *, is_subplan: bool) -> None:
        limit = self._settings.max_subplan_nodes if is_subplan else self._settings.max_plan_nodes
        if len(plan.nodes) > limit:
            scope = "Subplan" if is_subplan else "Plan"
            raise PlanValidationError(f"{scope} has {len(plan.nodes)} nodes; max is {limit}")

    def _check_tool_names(self, plan: Plan) -> None:
        tool_names = [n.tool for n in plan.nodes if n.type is NodeType.TOOL]
        unknown = self._harness.unknown_names(tool_names)
        if unknown:
            raise PlanValidationError(f"Plan references unknown tools: {unknown}")

    def _check_forced_tiers(self, plan: Plan) -> None:
        max_tier = len(self._settings.tiers) - 1
        for node in plan.nodes:
            if node.forced_tier is None:
                continue
            if node.forced_tier < 0 or node.forced_tier > max_tier:
                raise PlanValidationError(
                    f"Node {node.id} forced_tier={node.forced_tier} is outside "
                    f"configured tier range 0..{max_tier}"
                )

    def _topological_sort(self, plan: Plan) -> list[int]:
        """Kahn's algorithm with deterministic tie-breaking (ascending id).

        Raises if ``context_needed`` references a missing node or if the
        graph contains a cycle.
        """
        all_ids = {n.id for n in plan.nodes}
        in_degree: dict[int, int] = {n.id: 0 for n in plan.nodes}
        graph: dict[int, list[int]] = defaultdict(list)

        for node in plan.nodes:
            for dep in node.context_needed:
                if dep not in all_ids:
                    raise PlanValidationError(
                        f"Node {node.id} declares context_needed={dep} which doesn't exist"
                    )
                graph[dep].append(node.id)
                in_degree[node.id] += 1

        queue = sorted(sid for sid in all_ids if in_degree[sid] == 0)
        sorted_ids: list[int] = []

        while queue:
            node_id = queue.pop(0)
            sorted_ids.append(node_id)
            for neighbour in sorted(graph[node_id]):
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        if len(sorted_ids) != len(plan.nodes):
            raise PlanValidationError("Plan contains a dependency cycle")

        return sorted_ids

    def _check_decision_nesting(self, plan: Plan) -> None:
        node_map = {n.id: n for n in plan.nodes}
        limit = self._settings.max_decision_nesting_depth
        for node in plan.nodes:
            if node.type is not NodeType.DECISION or not node.branches:
                continue
            for branch_nodes in node.branches.values():
                depth = self._nesting_depth(branch_nodes, node_map, current=1)
                if depth > limit:
                    raise PlanValidationError(
                        f"Decision nesting depth {depth} exceeds max {limit} at node {node.id}"
                    )

    def _nesting_depth(
        self,
        node_ids: list[int],
        node_map: dict[int, Node],
        current: int,
    ) -> int:
        max_depth = current
        for nid in node_ids:
            node = node_map.get(nid)
            if node is None or node.type is not NodeType.DECISION or not node.branches:
                continue
            for branch_nodes in node.branches.values():
                depth = self._nesting_depth(branch_nodes, node_map, current + 1)
                max_depth = max(max_depth, depth)
        return max_depth

    def _check_subplans(self, plan: Plan) -> None:
        """Validate every SUBPLAN node's nested plan, and cap their count.

        Subplans are not recursively counted against the parent's
        ``max_subplans_per_plan`` — the limit is per-plan, not per-tree —
        but each nested plan is itself fully validated (which re-enters
        this same rule, so a subplan-of-a-subplan must also obey the
        per-plan cap).
        """
        subplan_nodes = [n for n in plan.nodes if n.type is NodeType.SUBPLAN]
        cap = self._settings.max_subplans_per_plan
        if len(subplan_nodes) > cap:
            raise PlanValidationError(f"Plan has {len(subplan_nodes)} subplan nodes; max is {cap}")
        for node in subplan_nodes:
            if node.subplan is None:
                raise PlanValidationError(f"SUBPLAN node {node.id} has no nested plan attached")
            self._check_node_limit(node.subplan, is_subplan=True)
            self._check_tool_names(node.subplan)
            self._check_forced_tiers(node.subplan)
            sub_sorted = self._topological_sort(node.subplan)
            self._check_decision_nesting(node.subplan)
            self._check_branch_references(node.subplan)
            self._check_subplans(node.subplan)
            node.subplan.sorted_node_ids = sub_sorted

    def _check_branch_references(self, plan: Plan) -> None:
        all_ids = {n.id for n in plan.nodes}
        for node in plan.nodes:
            if node.type is not NodeType.DECISION or not node.branches:
                continue
            for branch_name, branch_nodes in node.branches.items():
                for nid in branch_nodes:
                    if nid not in all_ids:
                        raise PlanValidationError(
                            f"Decision node {node.id} branch '{branch_name}' "
                            f"references non-existent node {nid}"
                        )
