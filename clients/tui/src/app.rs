//! App coordinator: holds transcript + UI state and reacts to terminal input
//! and agent messages. Rendering lives in `ui`; the wire lives in `acp`.

use std::collections::HashMap;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::acp::messages::{PermOption, PlanEntry, SessionUpdate};
use crate::acp::{AcpClient, ServerMessage};
use crate::transcript::Cell;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Ready,
    Working,
    AwaitingApproval,
}

/// Input focus / modal state.
pub enum Mode {
    Normal,
    Approval,
    Feedback(String),
}

struct PermissionState {
    id: Value,
    options: Vec<PermOption>,
    plan: Vec<PlanEntry>,
}

pub struct App {
    client: AcpClient,
    tx: mpsc::UnboundedSender<ServerMessage>,
    session_id: String,

    pub transcript: Vec<Cell>,
    pub plan: Vec<PlanEntry>,
    pub composer: String,
    pub status: Status,
    pub mode: Mode,
    pub should_quit: bool,

    permission: Option<PermissionState>,
    working: bool,
    tool_idx: HashMap<String, usize>,
}

impl App {
    pub fn new(
        client: AcpClient,
        tx: mpsc::UnboundedSender<ServerMessage>,
        session_id: String,
    ) -> Self {
        App {
            client,
            tx,
            session_id,
            transcript: vec![Cell::Notice(
                "Type a task and press Enter. Esc or Ctrl-C to quit.".into(),
            )],
            plan: Vec::new(),
            composer: String::new(),
            status: Status::Ready,
            mode: Mode::Normal,
            should_quit: false,
            permission: None,
            working: false,
            tool_idx: HashMap::new(),
        }
    }

    /// The options offered by the pending permission request (for the overlay).
    pub fn permission_options(&self) -> Option<&[PermOption]> {
        self.permission.as_ref().map(|p| p.options.as_slice())
    }

    /// The plan attached to the pending permission request (for the overlay).
    pub fn permission_plan(&self) -> Option<&[PlanEntry]> {
        self.permission.as_ref().map(|p| p.plan.as_slice())
    }

    // ── input ────────────────────────────────────────────────────────────

    pub async fn on_key(&mut self, key: KeyEvent) {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            self.should_quit = true;
            return;
        }
        match self.mode {
            Mode::Normal => self.on_key_normal(key).await,
            Mode::Approval => self.on_key_approval(key).await,
            Mode::Feedback(_) => self.on_key_feedback(key).await,
        }
    }

    async fn on_key_normal(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => {
                if self.working {
                    self.cancel_turn().await;
                } else {
                    self.should_quit = true;
                }
            }
            KeyCode::Enter => {
                if !self.working && !self.composer.trim().is_empty() {
                    self.submit().await;
                }
            }
            KeyCode::Backspace => {
                self.composer.pop();
            }
            KeyCode::Char(c) => self.composer.push(c),
            _ => {}
        }
    }

    async fn on_key_approval(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Char('a') => self.decide(approve(), Status::Working).await,
            KeyCode::Char('r') => self.decide(reject(None), Status::Working).await,
            KeyCode::Char('e') => self.mode = Mode::Feedback(String::new()),
            KeyCode::Esc => self.decide(cancel(), Status::Working).await,
            _ => {}
        }
    }

    async fn on_key_feedback(&mut self, key: KeyEvent) {
        let Mode::Feedback(buf) = &mut self.mode else {
            return;
        };
        match key.code {
            KeyCode::Enter => {
                let feedback = buf.clone();
                if feedback.trim().is_empty() {
                    self.mode = Mode::Approval;
                } else {
                    self.transcript
                        .push(Cell::Notice(format!("↩ revise: {feedback}")));
                    self.decide(reject(Some(feedback)), Status::Working).await;
                }
            }
            KeyCode::Esc => self.mode = Mode::Approval,
            KeyCode::Backspace => {
                buf.pop();
            }
            KeyCode::Char(c) => buf.push(c),
            _ => {}
        }
    }

    // ── agent messages ─────────────────────────────────────────────────────

    pub async fn on_server(&mut self, msg: ServerMessage) {
        match msg {
            ServerMessage::Update(update) => self.on_update(update),
            ServerMessage::Permission { id, params } => {
                self.permission = Some(PermissionState {
                    id,
                    options: params.options,
                    plan: params.plan.map(|p| p.entries).unwrap_or_default(),
                });
                self.mode = Mode::Approval;
                self.status = Status::AwaitingApproval;
            }
            ServerMessage::TurnEnded(reason) => {
                self.working = false;
                self.status = Status::Ready;
                self.transcript
                    .push(Cell::Notice(format!("— turn ended ({reason}) —")));
            }
            ServerMessage::Error(err) => {
                self.working = false;
                self.status = Status::Ready;
                self.transcript.push(Cell::Notice(format!("error: {err}")));
            }
            ServerMessage::AgentGone => {
                self.transcript
                    .push(Cell::Notice("agent process exited".into()));
                self.should_quit = true;
            }
        }
    }

    fn on_update(&mut self, update: SessionUpdate) {
        match update {
            SessionUpdate::Plan { entries } => self.plan = entries,
            SessionUpdate::ToolCall {
                tool_call_id,
                title,
                status,
            } => {
                let idx = self.transcript.len();
                self.transcript.push(Cell::Tool { title, status });
                self.tool_idx.insert(tool_call_id, idx);
            }
            SessionUpdate::ToolCallUpdate {
                tool_call_id,
                status,
            } => {
                if let Some(&idx) = self.tool_idx.get(&tool_call_id) {
                    if let Some(Cell::Tool { status: s, .. }) = self.transcript.get_mut(idx) {
                        *s = status;
                    }
                }
            }
            SessionUpdate::AgentMessageChunk { content } => {
                if !content.text.trim().is_empty() {
                    self.transcript.push(Cell::Message(content.text));
                }
            }
        }
    }

    // ── actions ──────────────────────────────────────────────────────────

    async fn submit(&mut self) {
        let prompt = std::mem::take(&mut self.composer);
        self.transcript.push(Cell::Prompt(prompt.clone()));
        self.plan.clear();
        self.tool_idx.clear();
        self.working = true;
        self.status = Status::Working;

        let client = self.client.clone();
        let tx = self.tx.clone();
        let session_id = self.session_id.clone();
        tokio::spawn(async move {
            let params = json!({
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            });
            match client.request("session/prompt", params).await {
                Ok(value) => {
                    let reason = value
                        .get("stopReason")
                        .and_then(Value::as_str)
                        .unwrap_or("end_turn")
                        .to_string();
                    let _ = tx.send(ServerMessage::TurnEnded(reason));
                }
                Err(err) => {
                    let _ = tx.send(ServerMessage::Error(err.to_string()));
                }
            }
        });
    }

    async fn cancel_turn(&mut self) {
        let _ = self
            .client
            .notify("session/cancel", json!({"sessionId": self.session_id}))
            .await;
        self.transcript
            .push(Cell::Notice("⎋ cancel requested".into()));
    }

    async fn decide(&mut self, outcome: Value, next: Status) {
        if let Some(state) = self.permission.take() {
            let _ = self.client.respond(state.id, outcome).await;
        }
        self.mode = Mode::Normal;
        self.status = next;
    }
}

fn approve() -> Value {
    json!({"outcome": {"outcome": "selected", "optionId": "approve"}})
}

fn reject(feedback: Option<String>) -> Value {
    match feedback {
        Some(text) => json!({
            "outcome": {"outcome": "selected", "optionId": "reject"},
            "feedback": text,
        }),
        None => json!({"outcome": {"outcome": "selected", "optionId": "reject"}}),
    }
}

fn cancel() -> Value {
    json!({"outcome": {"outcome": "cancelled"}})
}
