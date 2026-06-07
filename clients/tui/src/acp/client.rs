//! A minimal JSON-RPC 2.0 client over a child process's stdio.
//!
//! Speaks the client half of ACP to a spawned `glassrail acp` subprocess: sends
//! requests and notifications on the child's stdin, and a background reader task
//! demultiplexes the child's stdout into responses (correlated by id) and
//! server-initiated messages (notifications + `request_permission`) forwarded
//! to the app over a channel.
//!
//! This is intentionally hand-rolled rather than built on Zed's
//! `agent-client-protocol` crate: it keeps the dependency surface small and
//! lets the client mirror the agent exactly, including the free-text `feedback`
//! extension to the permission response. Swapping in the typed crate later is a
//! contained change behind this module.

use std::collections::HashMap;
use std::future::Future;
use std::process::Stdio;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot};

use crate::acp::messages::{PermissionParams, SessionUpdate};

/// A message the UI loop must react to: from the agent, or from a turn task.
#[derive(Debug)]
pub enum ServerMessage {
    /// A `session/update` notification we could parse into a known kind.
    Update(SessionUpdate),
    /// An agent→client `session/request_permission` request awaiting a reply.
    Permission { id: Value, params: PermissionParams },
    /// A `session/prompt` turn finished with this stop reason.
    TurnEnded(String),
    /// A request failed (transport or agent error).
    Error(String),
    /// The agent process exited / its stdout closed.
    AgentGone,
}

/// The client→agent calls the app makes. Abstracted from the concrete
/// stdio-backed [`AcpClient`] so the app's state machine can be tested against a
/// fake without spawning a subprocess.
pub trait Outbound: Clone + Send + Sync + 'static {
    fn request(&self, method: &str, params: Value) -> impl Future<Output = Result<Value>> + Send;
    fn notify(&self, method: &str, params: Value) -> impl Future<Output = Result<()>> + Send;
    fn respond(&self, id: Value, result: Value) -> impl Future<Output = Result<()>> + Send;
}

type Pending = Arc<Mutex<HashMap<i64, oneshot::Sender<Result<Value>>>>>;

/// Cheap-to-clone handle to the running agent connection.
#[derive(Clone)]
pub struct AcpClient {
    stdin: Arc<tokio::sync::Mutex<ChildStdin>>,
    next_id: Arc<AtomicI64>,
    pending: Pending,
}

impl AcpClient {
    /// Spawn the agent command and wire up the reader task.
    ///
    /// `command` is the argv to run (e.g. `["glassrail", "acp"]`). Server-initiated
    /// messages are forwarded on `tx`, which the caller also clones for turn tasks.
    pub fn spawn(
        command: &[String],
        tx: mpsc::UnboundedSender<ServerMessage>,
    ) -> Result<(Self, Child)> {
        let (program, args) = command
            .split_first()
            .ok_or_else(|| anyhow!("empty agent command"))?;
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .with_context(|| format!("failed to spawn agent: {program}"))?;

        let stdin = child.stdin.take().context("child stdin missing")?;
        let stdout = child.stdout.take().context("child stdout missing")?;

        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));

        let reader_pending = Arc::clone(&pending);
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                if let Ok(value) = serde_json::from_str::<Value>(trimmed) {
                    route_incoming(value, &reader_pending, &tx);
                }
            }
            let _ = tx.send(ServerMessage::AgentGone);
        });

        let client = AcpClient {
            stdin: Arc::new(tokio::sync::Mutex::new(stdin)),
            next_id: Arc::new(AtomicI64::new(0)),
            pending,
        };
        Ok((client, child))
    }

    async fn write(&self, msg: &Value) -> Result<()> {
        let mut line = serde_json::to_string(msg)?;
        line.push('\n');
        let mut guard = self.stdin.lock().await;
        guard.write_all(line.as_bytes()).await?;
        guard.flush().await?;
        Ok(())
    }
}

impl Outbound for AcpClient {
    /// Send a request and await its response result.
    async fn request(&self, method: &str, params: Value) -> Result<Value> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst) + 1;
        let (otx, orx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, otx);
        let msg = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        if let Err(e) = self.write(&msg).await {
            self.pending.lock().unwrap().remove(&id);
            return Err(e);
        }
        orx.await.context("response channel closed")?
    }

    /// Send a notification (no response expected), e.g. `session/cancel`.
    async fn notify(&self, method: &str, params: Value) -> Result<()> {
        let msg = json!({"jsonrpc": "2.0", "method": method, "params": params});
        self.write(&msg).await
    }

    /// Reply to an agent→client request (e.g. a permission decision).
    async fn respond(&self, id: Value, result: Value) -> Result<()> {
        let msg = json!({"jsonrpc": "2.0", "id": id, "result": result});
        self.write(&msg).await
    }
}

/// Classify one inbound message: response to us, or a server-initiated message.
fn route_incoming(value: Value, pending: &Pending, tx: &mpsc::UnboundedSender<ServerMessage>) {
    let method = value.get("method").and_then(Value::as_str);
    let has_id = value.get("id").is_some();

    match (method, has_id) {
        // Response to one of our requests.
        (None, true) => {
            if let Some(id) = value.get("id").and_then(Value::as_i64) {
                if let Some(sender) = pending.lock().unwrap().remove(&id) {
                    let result = if let Some(err) = value.get("error") {
                        Err(anyhow!("agent error: {err}"))
                    } else {
                        Ok(value.get("result").cloned().unwrap_or(Value::Null))
                    };
                    let _ = sender.send(result);
                }
            }
        }
        // Agent→client request (we only handle request_permission).
        (Some("session/request_permission"), true) => {
            let id = value.get("id").cloned().unwrap_or(Value::Null);
            let params = value
                .get("params")
                .cloned()
                .and_then(|p| serde_json::from_value::<PermissionParams>(p).ok())
                .unwrap_or(PermissionParams {
                    plan: None,
                    tool_call: None,
                    options: Vec::new(),
                });
            let _ = tx.send(ServerMessage::Permission { id, params });
        }
        // Notification.
        (Some("session/update"), false) => {
            if let Some(update) = value.get("params").and_then(|p| p.get("update")) {
                if let Ok(parsed) = serde_json::from_value::<SessionUpdate>(update.clone()) {
                    let _ = tx.send(ServerMessage::Update(parsed));
                }
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn routes_plan_update_to_app() {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (tx, mut rx) = mpsc::unbounded_channel();
        let value = json!({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "plan",
                    "entries": [{"content": "do x", "status": "pending"}],
                },
            },
        });
        route_incoming(value, &pending, &tx);
        match rx.recv().await.unwrap() {
            ServerMessage::Update(SessionUpdate::Plan { entries }) => {
                assert_eq!(entries.len(), 1);
                assert_eq!(entries[0].content, "do x");
            }
            other => panic!("expected plan update, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn routes_message_chunk_to_app() {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (tx, mut rx) = mpsc::unbounded_channel();
        let value = json!({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "the answer"},
                    "nodeId": 2,
                    "nodeType": "result",
                    "isFinal": true,
                },
            },
        });
        route_incoming(value, &pending, &tx);
        match rx.recv().await.unwrap() {
            ServerMessage::Update(SessionUpdate::AgentMessageChunk {
                content,
                node_id,
                node_type,
                is_final,
            }) => {
                assert_eq!(content.text, "the answer");
                assert_eq!(node_id, Some(2));
                assert_eq!(node_type, "result");
                assert!(is_final);
            }
            other => panic!("expected message chunk, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn routes_response_to_pending_request() {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (tx, _rx) = mpsc::unbounded_channel();
        let (otx, orx) = oneshot::channel();
        pending.lock().unwrap().insert(7, otx);
        let value = json!({"jsonrpc": "2.0", "id": 7, "result": {"sessionId": "abc"}});
        route_incoming(value, &pending, &tx);
        let result = orx.await.unwrap().unwrap();
        assert_eq!(result["sessionId"], "abc");
    }

    #[tokio::test]
    async fn routes_permission_request_with_options() {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (tx, mut rx) = mpsc::unbounded_channel();
        let value = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/request_permission",
            "params": {
                "plan": {"entries": [{"content": "step", "status": "pending"}]},
                "options": [{"optionId": "approve", "name": "Approve"}],
            },
        });
        route_incoming(value, &pending, &tx);
        match rx.recv().await.unwrap() {
            ServerMessage::Permission { id, params } => {
                assert_eq!(id, json!(1));
                assert_eq!(params.options.len(), 1);
                assert_eq!(params.options[0].option_id, "approve");
                assert_eq!(params.plan.unwrap().entries.len(), 1);
            }
            other => panic!("expected permission, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn ignores_unknown_update_kind() {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (tx, mut rx) = mpsc::unbounded_channel();
        let value = json!({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "s", "update": {"sessionUpdate": "future_thing"}},
        });
        route_incoming(value, &pending, &tx);
        assert!(rx.try_recv().is_err(), "unknown update kinds are dropped");
    }
}
