//! The plan graph the DAG view renders.
//!
//! ACP's `plan` update is a flat list, so the agent sends the topology
//! separately (the `plan_graph` extension). Here we turn those wire nodes into
//! laid-out nodes: each assigned a topological *layer* (= longest path from a
//! root over `deps`), so nodes sharing a layer have no dependency between them
//! and form a parallel cohort. Edges/connectors are not drawn yet.
//!
//! The layering mirrors `_layers` in the Python `gateways/tui/dag.py`.

use std::collections::{HashMap, HashSet};

use crate::acp::messages::GraphNode as WireNode;

#[derive(Debug, Clone)]
pub struct GraphNode {
    pub id: i64,
    pub node_type: String,
    pub description: String,
    pub layer: usize,
    pub status: String,
}

/// Build laid-out graph nodes (with layers) from the wire nodes, in input order.
pub fn build(nodes: Vec<WireNode>) -> Vec<GraphNode> {
    let ids: HashSet<i64> = nodes.iter().map(|n| n.id).collect();
    let parents: HashMap<i64, Vec<i64>> = nodes
        .iter()
        .map(|n| {
            let deps = n.deps.iter().copied().filter(|d| ids.contains(d)).collect();
            (n.id, deps)
        })
        .collect();

    let mut depth: HashMap<i64, usize> = HashMap::new();
    let mut visiting: HashSet<i64> = HashSet::new();
    for n in &nodes {
        layer_of(n.id, &parents, &mut depth, &mut visiting);
    }

    nodes
        .into_iter()
        .map(|n| GraphNode {
            layer: depth.get(&n.id).copied().unwrap_or(0),
            id: n.id,
            node_type: n.node_type,
            description: n.description,
            status: "pending".to_string(),
        })
        .collect()
}

/// Longest path from any root to `id` over `parents`. Memoised; the `visiting`
/// set guards against a malformed cyclic plan (returns 0 rather than recursing).
fn layer_of(
    id: i64,
    parents: &HashMap<i64, Vec<i64>>,
    depth: &mut HashMap<i64, usize>,
    visiting: &mut HashSet<i64>,
) -> usize {
    if let Some(&d) = depth.get(&id) {
        return d;
    }
    if !visiting.insert(id) {
        return 0; // cycle guard
    }
    let ps = parents.get(&id).cloned().unwrap_or_default();
    let d = ps
        .iter()
        .map(|p| layer_of(*p, parents, depth, visiting))
        .max()
        .map(|m| m + 1)
        .unwrap_or(0);
    visiting.remove(&id);
    depth.insert(id, d);
    d
}

/// The highest layer index present (0 for an empty graph).
pub fn max_layer(nodes: &[GraphNode]) -> usize {
    nodes.iter().map(|n| n.layer).max().unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wire(id: i64, deps: &[i64]) -> WireNode {
        WireNode {
            id,
            node_type: "tool".into(),
            description: format!("node {id}"),
            deps: deps.to_vec(),
        }
    }

    #[test]
    fn chain_layers_increase() {
        let g = build(vec![wire(1, &[]), wire(2, &[1]), wire(3, &[2])]);
        assert_eq!(g.iter().map(|n| n.layer).collect::<Vec<_>>(), vec![0, 1, 2]);
    }

    #[test]
    fn diamond_shares_a_layer() {
        // 1 → {2, 3} → 4: nodes 2 and 3 are a parallel cohort on layer 1.
        let g = build(vec![
            wire(1, &[]),
            wire(2, &[1]),
            wire(3, &[1]),
            wire(4, &[2, 3]),
        ]);
        let layer = |id: i64| g.iter().find(|n| n.id == id).unwrap().layer;
        assert_eq!(layer(1), 0);
        assert_eq!(layer(2), 1);
        assert_eq!(layer(3), 1);
        assert_eq!(layer(4), 2);
        assert_eq!(max_layer(&g), 2);
    }

    #[test]
    fn unknown_deps_are_ignored() {
        let g = build(vec![wire(1, &[99]), wire(2, &[1])]);
        assert_eq!(g[0].layer, 0); // dep 99 doesn't exist → root
        assert_eq!(g[1].layer, 1);
    }
}
