//! Typed payloads for the slice of ACP the client consumes.
//!
//! Only the fields the TUI renders are modelled; unknown fields are ignored so
//! the client tolerates a richer agent. Deserialisation of `session/update` is
//! best-effort: an unrecognised `sessionUpdate` kind parses to `None` rather
//! than erroring the stream.

use serde::Deserialize;

/// One entry in an ACP plan update (re-sent in full on every change).
/// Only the fields the TUI renders are modelled; `priority` etc. are ignored.
#[derive(Debug, Clone, Deserialize)]
pub struct PlanEntry {
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
    },
    ToolCallUpdate {
        #[serde(rename = "toolCallId")]
        tool_call_id: String,
        #[serde(default)]
        status: String,
    },
    AgentMessageChunk {
        content: Content,
    },
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
    #[serde(default)]
    pub options: Vec<PermOption>,
}
