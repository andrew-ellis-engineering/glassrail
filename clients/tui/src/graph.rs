//! The plan graph the DAG view renders.
//!
//! ACP's `plan` update is a flat list, so glassrail sends topology separately
//! via the `plan_graph` extension. This module turns that wire topology into a
//! layered graph layout: nodes get dependency layers, long edges are split with
//! dummy pass-through vertices, and every rendered connector spans adjacent
//! layers only.

use std::collections::{HashMap, HashSet};

use crate::acp::messages::{GraphEdge as WireEdge, GraphNode as WireNode};

/// Width of a rendered node box, in terminal cells.
pub const BOX_W: usize = 24;
/// Height of a rendered node box, in terminal cells.
pub const BOX_H: usize = 4;
/// Horizontal space between boxes in the same layer.
pub const COL_GAP: usize = 4;
/// Rows between two layers, used as the connector channel.
pub const CHANNEL: usize = 2;

#[derive(Debug, Clone, Default)]
pub struct Graph {
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<GraphEdge>,
}

impl Graph {
    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }

    pub fn node(&self, id: i64) -> Option<&GraphNode> {
        self.nodes.iter().find(|n| n.id == id)
    }

    pub fn update_status(&mut self, node_id: i64, status: String) {
        if let Some(node) = self.nodes.iter_mut().find(|n| n.id == node_id) {
            node.status = status;
        }
    }
}

#[derive(Debug, Clone)]
pub struct GraphNode {
    pub id: i64,
    pub node_type: String,
    pub description: String,
    pub layer: usize,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphEdge {
    pub from: i64,
    pub to: i64,
    pub kind: EdgeKind,
    pub label: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeKind {
    Data,
    Control,
}

impl EdgeKind {
    fn from_wire(value: &str) -> Self {
        match value {
            "control" => EdgeKind::Control,
            _ => EdgeKind::Data,
        }
    }
}

#[derive(Debug, Clone)]
pub struct GraphLayout {
    /// Vertex centre x coordinate.
    pub vx: HashMap<i64, usize>,
    /// Real node/dummy left x coordinate.
    pub vleft: HashMap<i64, usize>,
    /// Vertex layer index.
    pub vlayer: HashMap<i64, usize>,
    /// Adjacent-layer connector segments.
    pub segments: Vec<GraphSegment>,
    pub width: usize,
    pub height: usize,
}

#[derive(Debug, Clone)]
pub struct GraphSegment {
    pub from: i64,
    pub to: i64,
    pub kind: EdgeKind,
    pub label: Option<String>,
}

/// Build a laid-out graph from wire nodes/edges. If explicit edges are absent,
/// derive data edges from each node's legacy `deps` field.
pub fn build(nodes: Vec<WireNode>, edges: Vec<WireEdge>) -> Graph {
    let ids: HashSet<i64> = nodes.iter().map(|n| n.id).collect();
    let graph_edges = normalize_edges(&nodes, edges, &ids);
    let parents = parents_from_edges(&nodes, &graph_edges);

    let mut depth: HashMap<i64, usize> = HashMap::new();
    let mut visiting: HashSet<i64> = HashSet::new();
    for n in &nodes {
        layer_of(n.id, &parents, &mut depth, &mut visiting);
    }

    let graph_nodes = nodes
        .into_iter()
        .map(|n| GraphNode {
            layer: depth.get(&n.id).copied().unwrap_or(0),
            id: n.id,
            node_type: n.node_type,
            description: n.description,
            status: "pending".to_string(),
        })
        .collect();

    Graph {
        nodes: graph_nodes,
        edges: graph_edges,
    }
}

fn normalize_edges(nodes: &[WireNode], edges: Vec<WireEdge>, ids: &HashSet<i64>) -> Vec<GraphEdge> {
    let mut out: Vec<GraphEdge> = if edges.is_empty() {
        nodes
            .iter()
            .flat_map(|node| {
                node.deps.iter().filter_map(|dep| {
                    ids.contains(dep).then_some(GraphEdge {
                        from: *dep,
                        to: node.id,
                        kind: EdgeKind::Data,
                        label: None,
                    })
                })
            })
            .collect()
    } else {
        edges
            .into_iter()
            .filter_map(|edge| {
                (ids.contains(&edge.from) && ids.contains(&edge.to)).then_some(GraphEdge {
                    from: edge.from,
                    to: edge.to,
                    kind: EdgeKind::from_wire(&edge.kind),
                    label: edge.label.filter(|label| !label.trim().is_empty()),
                })
            })
            .collect()
    };
    out.sort_by_key(|edge| (edge.from, edge.to, edge.kind == EdgeKind::Control));
    out.dedup_by(|a, b| a.from == b.from && a.to == b.to && a.kind == b.kind && a.label == b.label);
    out
}

fn parents_from_edges(nodes: &[WireNode], edges: &[GraphEdge]) -> HashMap<i64, Vec<i64>> {
    let mut parents: HashMap<i64, Vec<i64>> = nodes.iter().map(|n| (n.id, Vec::new())).collect();
    for edge in edges {
        parents.entry(edge.to).or_default().push(edge.from);
    }
    parents
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
pub fn max_layer(graph: &Graph) -> usize {
    graph.nodes.iter().map(|n| n.layer).max().unwrap_or(0)
}

pub fn layout(graph: &Graph) -> GraphLayout {
    let mut vlayer: HashMap<i64, usize> = graph.nodes.iter().map(|n| (n.id, n.layer)).collect();
    let mut items: Vec<Vec<i64>> = vec![Vec::new(); max_layer(graph) + 1];
    for node in &graph.nodes {
        items[node.layer].push(node.id);
    }

    let mut segments: Vec<GraphSegment> = Vec::new();
    let mut next_dummy = -1;
    for edge in &graph.edges {
        let Some(&from_layer) = vlayer.get(&edge.from) else {
            continue;
        };
        let Some(&to_layer) = vlayer.get(&edge.to) else {
            continue;
        };
        if to_layer <= from_layer {
            continue;
        }
        let mut prev = edge.from;
        for layer_idx in (from_layer + 1)..to_layer {
            let dummy = next_dummy;
            next_dummy -= 1;
            if let Some(layer) = items.get_mut(layer_idx) {
                layer.push(dummy);
            }
            vlayer.insert(dummy, layer_idx);
            segments.push(GraphSegment {
                from: prev,
                to: dummy,
                kind: edge.kind,
                label: None,
            });
            prev = dummy;
        }
        segments.push(GraphSegment {
            from: prev,
            to: edge.to,
            kind: edge.kind,
            label: edge.label.clone(),
        });
    }

    let mut vx: HashMap<i64, usize> = HashMap::new();
    let mut vleft: HashMap<i64, usize> = HashMap::new();
    let mut layer_widths: Vec<usize> = Vec::new();
    for row in &items {
        let mut cursor = 0usize;
        for item in row {
            let width = if *item > 0 { BOX_W } else { 1 };
            vleft.insert(*item, cursor);
            vx.insert(*item, cursor + width / 2);
            cursor += width + COL_GAP;
        }
        layer_widths.push(cursor.saturating_sub(COL_GAP));
    }

    let width = layer_widths.iter().copied().max().unwrap_or(0);
    for (idx, row) in items.iter().enumerate() {
        let offset = width.saturating_sub(layer_widths[idx]) / 2;
        for item in row {
            if let Some(left) = vleft.get_mut(item) {
                *left += offset;
            }
            if let Some(center) = vx.get_mut(item) {
                *center += offset;
            }
        }
    }

    let height = items.len() * BOX_H + items.len().saturating_sub(1) * CHANNEL;
    GraphLayout {
        vx,
        vleft,
        vlayer,
        segments,
        width,
        height,
    }
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

    fn edge(from: i64, to: i64, kind: &str) -> WireEdge {
        WireEdge {
            from,
            to,
            kind: kind.into(),
            label: None,
        }
    }

    #[test]
    fn chain_layers_increase() {
        let g = build(vec![wire(1, &[]), wire(2, &[1]), wire(3, &[2])], Vec::new());
        assert_eq!(
            g.nodes.iter().map(|n| n.layer).collect::<Vec<_>>(),
            vec![0, 1, 2]
        );
    }

    #[test]
    fn diamond_shares_a_layer() {
        // 1 -> {2, 3} -> 4: nodes 2 and 3 are a parallel cohort on layer 1.
        let g = build(
            vec![wire(1, &[]), wire(2, &[1]), wire(3, &[1]), wire(4, &[2, 3])],
            Vec::new(),
        );
        let layer = |id: i64| g.nodes.iter().find(|n| n.id == id).unwrap().layer;
        assert_eq!(layer(1), 0);
        assert_eq!(layer(2), 1);
        assert_eq!(layer(3), 1);
        assert_eq!(layer(4), 2);
        assert_eq!(max_layer(&g), 2);
        assert_eq!(g.edges.len(), 4);
    }

    #[test]
    fn explicit_control_edges_affect_layers() {
        let g = build(
            vec![wire(1, &[]), wire(2, &[])],
            vec![WireEdge {
                from: 1,
                to: 2,
                kind: "control".into(),
                label: Some("yes".into()),
            }],
        );

        assert_eq!(g.nodes[0].layer, 0);
        assert_eq!(g.nodes[1].layer, 1);
        assert_eq!(g.edges[0].kind, EdgeKind::Control);
        assert_eq!(g.edges[0].label.as_deref(), Some("yes"));
    }

    #[test]
    fn unknown_deps_and_edges_are_ignored() {
        let g = build(
            vec![wire(1, &[99]), wire(2, &[1])],
            vec![edge(42, 2, "data")],
        );
        assert_eq!(g.nodes[0].layer, 0);
        assert_eq!(g.nodes[1].layer, 0);
        assert!(g.edges.is_empty());
    }

    #[test]
    fn layout_splits_long_edges_with_dummy_vertices() {
        let g = build(
            vec![wire(1, &[]), wire(2, &[1]), wire(3, &[2]), wire(4, &[1])],
            vec![
                edge(1, 2, "data"),
                edge(2, 3, "data"),
                edge(3, 4, "data"),
                edge(1, 4, "data"),
            ],
        );

        let layout = layout(&g);

        assert!(layout.vlayer.values().any(|layer| *layer == 1));
        assert!(layout.segments.iter().any(|segment| segment.to < 0));
        assert!(layout.segments.iter().any(|segment| segment.from < 0));
    }
}
