//! Typed payloads for the slice of ACP the client consumes.
//!
//! Only the fields the TUI renders are modelled; unknown fields are ignored so
//! the client tolerates a richer agent. Deserialisation of `session/update` is
//! best-effort: an unrecognised `sessionUpdate` kind parses to `None` rather
//! than erroring the stream.

use serde::Deserialize;
use serde_json::Value;

/// One entry in an ACP plan update (re-sent in full on every change).
/// Only the fields the TUI renders are modelled; `priority` etc. are ignored.
#[derive(Debug, Clone, Deserialize)]
pub struct PlanEntry {
    #[serde(default, rename = "nodeId")]
    pub node_id: Option<i64>,
    pub content: String,
    pub status: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Content {
    #[serde(default)]
    pub text: String,
}

/// A `session/update` notification's `update` object, tagged by `sessionUpdate`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "sessionUpdate", rename_all = "snake_case")]
pub enum SessionUpdate {
    Plan {
        entries: Vec<PlanEntry>,
    },
    ToolCall {
        #[serde(rename = "toolCallId")]
        tool_call_id: String,
        #[serde(default)]
        title: String,
        #[serde(default)]
        status: String,
        #[serde(default, rename = "rawInput")]
        raw_input: Value,
    },
    ToolCallUpdate {
        #[serde(rename = "toolCallId")]
        tool_call_id: String,
        #[serde(default)]
        status: String,
        #[serde(default, rename = "rawOutput")]
        raw_output: Option<Value>,
    },
    AgentMessageChunk {
        content: Content,
        /// dagagent extension: node id for intermediate node output chunks.
        #[serde(default, rename = "nodeId")]
        node_id: Option<i64>,
        /// dagagent extension: node type for intermediate/final text.
        #[serde(default, rename = "nodeType")]
        node_type: String,
        /// dagagent extension: true when this chunk is the final task result.
        #[serde(default, rename = "isFinal")]
        is_final: bool,
    },
    /// A dagagent extension: per-node tier/confidence metadata. Standard ACP
    /// clients ignore unknown update kinds; ours renders a dim annotation.
    NodeMeta {
        #[serde(default, rename = "nodeType")]
        node_type: String,
        #[serde(default)]
        tier: Option<u32>,
        #[serde(default)]
        confidence: f64,
        #[serde(default)]
        flagged: bool,
    },
    /// A dagagent extension: the plan's graph topology (ACP's flat plan omits
    /// edges), used to render the DAG view. Sent once per plan.
    PlanGraph {
        #[serde(default)]
        nodes: Vec<GraphNode>,
        #[serde(default)]
        edges: Vec<GraphEdge>,
    },
}

/// One node of the plan graph, as sent on the wire.
#[derive(Debug, Clone, Deserialize)]
pub struct GraphNode {
    pub id: i64,
    #[serde(default, rename = "nodeType")]
    pub node_type: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub deps: Vec<i64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GraphEdge {
    pub from: i64,
    pub to: i64,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub label: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlanWrap {
    #[serde(default)]
    pub entries: Vec<PlanEntry>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PermOption {
    #[serde(rename = "optionId")]
    pub option_id: String,
    #[serde(default)]
    pub name: String,
}

/// Params of an agent→client `session/request_permission` request.
#[derive(Debug, Clone, Deserialize)]
pub struct PermissionParams {
    #[serde(default)]
    pub plan: Option<PlanWrap>,
    #[serde(default, rename = "toolCall")]
    pub tool_call: Option<ToolCallPermission>,
    #[serde(default)]
    pub options: Vec<PermOption>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ToolCallPermission {
    #[serde(default, rename = "toolName")]
    pub tool_name: String,
    #[serde(default)]
    pub risk: String,
    #[serde(default)]
    pub args: serde_json::Value,
    #[serde(default)]
    pub description: String,
}
