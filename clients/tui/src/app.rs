//! App coordinator: holds transcript + UI state and reacts to terminal input
//! and agent messages. Rendering lives in `ui`; the wire lives in `acp`.

use std::collections::HashMap;
use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::acp::messages::{PermOption, PlanEntry, SessionUpdate};
use crate::acp::{Outbound, ServerMessage};
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

pub struct App<O: Outbound> {
    client: O,
    tx: mpsc::UnboundedSender<ServerMessage>,
    session_id: String,

    pub transcript: Vec<Cell>,
    pub plan: Vec<PlanEntry>,
    pub composer: String,
    pub status: Status,
    pub mode: Mode,
    pub should_quit: bool,
    /// Lines scrolled up from the tail; 0 follows the latest output.
    pub scrollback: u16,
    /// Spinner animation frame, advanced on each tick while working.
    pub spinner: usize,
    /// When the current turn started, for the elapsed-time readout.
    pub turn_start: Option<Instant>,

    permission: Option<PermissionState>,
    working: bool,
    tool_idx: HashMap<String, usize>,
}

/// Spinner frames shown in the status line while a turn runs.
pub const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

impl<O: Outbound> App<O> {
    pub fn new(client: O, tx: mpsc::UnboundedSender<ServerMessage>, session_id: String) -> Self {
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
            scrollback: 0,
            spinner: 0,
            turn_start: None,
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

    /// Advance the spinner one frame (called on the UI tick while working).
    pub fn tick(&mut self) {
        self.spinner = self.spinner.wrapping_add(1);
    }

    /// Tick only while a turn is running, so the spinner is still when idle.
    pub fn tick_if_working(&mut self) {
        if self.status == Status::Working {
            self.tick();
        }
    }

    /// Scroll the transcript up (toward older output) or down (toward the tail).
    pub fn scroll(&mut self, up: bool, lines: u16) {
        self.scrollback = if up {
            self.scrollback.saturating_add(lines)
        } else {
            self.scrollback.saturating_sub(lines)
        };
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
            KeyCode::Up => self.scroll(true, 1),
            KeyCode::Down => self.scroll(false, 1),
            KeyCode::PageUp => self.scroll(true, 10),
            KeyCode::PageDown => self.scroll(false, 10),
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
                let elapsed = self.turn_start.take().map(|t| t.elapsed().as_secs());
                self.working = false;
                self.status = Status::Ready;
                let suffix = elapsed.map(|s| format!(" in {s}s")).unwrap_or_default();
                self.transcript
                    .push(Cell::Notice(format!("— turn ended ({reason}){suffix} —")));
            }
            ServerMessage::Error(err) => {
                self.turn_start = None;
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
                raw_input,
            } => {
                let idx = self.transcript.len();
                self.transcript.push(Cell::Tool {
                    title,
                    args: compact_args(&raw_input),
                    status,
                    output: None,
                });
                self.tool_idx.insert(tool_call_id, idx);
            }
            SessionUpdate::ToolCallUpdate {
                tool_call_id,
                status,
                raw_output,
            } => {
                if let Some(&idx) = self.tool_idx.get(&tool_call_id) {
                    if let Some(Cell::Tool {
                        status: s, output, ..
                    }) = self.transcript.get_mut(idx)
                    {
                        *s = status;
                        if let Some(text) = raw_output.as_ref().and_then(extract_output) {
                            *output = Some(text);
                        }
                    }
                }
            }
            SessionUpdate::AgentMessageChunk { content } => {
                if !content.text.trim().is_empty() {
                    self.transcript.push(Cell::Message(content.text));
                }
            }
            SessionUpdate::NodeMeta {
                node_type,
                tier,
                confidence,
                flagged,
            } => {
                // The result node's metadata (always conf 1.0) is noise; skip it.
                if node_type != "result" {
                    let tier = tier.map(|t| t.to_string()).unwrap_or_else(|| "?".into());
                    let flag = if flagged { "  ⚑ flagged" } else { "" };
                    self.transcript.push(Cell::Meta(format!(
                        "tier {tier} · conf {confidence:.2}{flag}"
                    )));
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
        self.scrollback = 0; // jump back to the tail for the new turn
        self.working = true;
        self.status = Status::Working;
        self.turn_start = Some(Instant::now());

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

const PREVIEW_MAX: usize = 120;

/// Render a tool's raw input object as a compact `k=v, k=v` argument string.
fn compact_args(raw: &Value) -> String {
    let Some(obj) = raw.as_object() else {
        return String::new();
    };
    let parts: Vec<String> = obj
        .iter()
        .map(|(k, v)| match v {
            Value::String(s) => format!("{k}={s}"),
            other => format!("{k}={other}"),
        })
        .collect();
    truncate(&parts.join(", "))
}

/// Pull a tool's textual output from its rawOutput object (or stringify it).
fn extract_output(raw: &Value) -> Option<String> {
    let text = match raw.get("output") {
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
        None => raw.to_string(),
    };
    let trimmed = text.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(truncate(trimmed))
    }
}

fn truncate(s: &str) -> String {
    let one_line = s.replace('\n', " ");
    match one_line.char_indices().nth(PREVIEW_MAX) {
        Some((idx, _)) => format!("{}…", &one_line[..idx]),
        None => one_line,
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use anyhow::Result;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    use super::*;
    use crate::acp::messages::{Content, PermOption, PermissionParams, PlanEntry, PlanWrap};

    #[derive(Clone, Default)]
    struct FakeOutbound {
        requests: Arc<Mutex<Vec<(String, Value)>>>,
        notifications: Arc<Mutex<Vec<(String, Value)>>>,
        responses: Arc<Mutex<Vec<(Value, Value)>>>,
    }

    impl Outbound for FakeOutbound {
        async fn request(&self, method: &str, params: Value) -> Result<Value> {
            self.requests
                .lock()
                .unwrap()
                .push((method.to_string(), params));
            Ok(json!({"stopReason": "end_turn"}))
        }
        async fn notify(&self, method: &str, params: Value) -> Result<()> {
            self.notifications
                .lock()
                .unwrap()
                .push((method.to_string(), params));
            Ok(())
        }
        async fn respond(&self, id: Value, result: Value) -> Result<()> {
            self.responses.lock().unwrap().push((id, result));
            Ok(())
        }
    }

    type TestApp = App<FakeOutbound>;

    fn app() -> (
        TestApp,
        FakeOutbound,
        mpsc::UnboundedReceiver<ServerMessage>,
    ) {
        let client = FakeOutbound::default();
        let (tx, rx) = mpsc::unbounded_channel();
        (App::new(client.clone(), tx, "sess-1".into()), client, rx)
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::empty())
    }

    fn permission() -> ServerMessage {
        ServerMessage::Permission {
            id: json!(7),
            params: PermissionParams {
                plan: Some(PlanWrap {
                    entries: vec![PlanEntry {
                        content: "step one".into(),
                        status: "pending".into(),
                    }],
                }),
                options: vec![PermOption {
                    option_id: "approve".into(),
                    name: "Approve".into(),
                }],
            },
        }
    }

    fn last_response(client: &FakeOutbound) -> Value {
        client.responses.lock().unwrap().last().unwrap().1.clone()
    }

    #[tokio::test]
    async fn submit_starts_a_turn_and_records_the_prompt() {
        let (mut app, client, mut rx) = app();
        app.composer = "do the thing".into();
        app.on_key(key(KeyCode::Enter)).await;

        assert_eq!(app.status, Status::Working);
        assert!(matches!(app.transcript.last(), Some(Cell::Prompt(p)) if p == "do the thing"));
        assert!(app.composer.is_empty());

        // The spawned turn task issues session/prompt and reports completion.
        let msg = rx.recv().await.unwrap();
        assert!(matches!(msg, ServerMessage::TurnEnded(reason) if reason == "end_turn"));
        let reqs = client.requests.lock().unwrap();
        assert_eq!(reqs[0].0, "session/prompt");
    }

    #[tokio::test]
    async fn empty_prompt_does_not_submit() {
        let (mut app, client, _rx) = app();
        app.composer = "   ".into();
        app.on_key(key(KeyCode::Enter)).await;
        assert_eq!(app.status, Status::Ready);
        assert!(client.requests.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn permission_enters_approval_mode() {
        let (mut app, _client, _rx) = app();
        app.on_server(permission()).await;
        assert_eq!(app.status, Status::AwaitingApproval);
        assert!(matches!(app.mode, Mode::Approval));
        assert_eq!(app.permission_options().unwrap()[0].option_id, "approve");
        assert_eq!(app.permission_plan().unwrap()[0].content, "step one");
    }

    #[tokio::test]
    async fn approve_responds_and_resumes_working() {
        let (mut app, client, _rx) = app();
        app.on_server(permission()).await;
        app.on_key(key(KeyCode::Char('a'))).await;

        assert_eq!(
            last_response(&client),
            json!({"outcome": {"outcome": "selected", "optionId": "approve"}})
        );
        assert_eq!(app.status, Status::Working);
        assert!(matches!(app.mode, Mode::Normal));
    }

    #[tokio::test]
    async fn reject_without_feedback_responds_reject() {
        let (mut app, client, _rx) = app();
        app.on_server(permission()).await;
        app.on_key(key(KeyCode::Char('r'))).await;
        assert_eq!(
            last_response(&client),
            json!({"outcome": {"outcome": "selected", "optionId": "reject"}})
        );
    }

    #[tokio::test]
    async fn reject_with_feedback_threads_text() {
        let (mut app, client, _rx) = app();
        app.on_server(permission()).await;
        app.on_key(key(KeyCode::Char('e'))).await;
        assert!(matches!(app.mode, Mode::Feedback(_)));
        for c in "shorter".chars() {
            app.on_key(key(KeyCode::Char(c))).await;
        }
        app.on_key(key(KeyCode::Enter)).await;

        assert_eq!(
            last_response(&client),
            json!({"outcome": {"outcome": "selected", "optionId": "reject"}, "feedback": "shorter"})
        );
        assert!(app
            .transcript
            .iter()
            .any(|c| matches!(c, Cell::Notice(n) if n.contains("shorter"))));
    }

    #[tokio::test]
    async fn esc_in_approval_cancels() {
        let (mut app, client, _rx) = app();
        app.on_server(permission()).await;
        app.on_key(key(KeyCode::Esc)).await;
        assert_eq!(
            last_response(&client),
            json!({"outcome": {"outcome": "cancelled"}})
        );
    }

    #[tokio::test]
    async fn plan_and_tool_updates_render() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::Plan {
            entries: vec![PlanEntry {
                content: "do x".into(),
                status: "in_progress".into(),
            }],
        }))
        .await;
        assert_eq!(app.plan[0].content, "do x");

        app.on_server(ServerMessage::Update(SessionUpdate::ToolCall {
            tool_call_id: "node-1".into(),
            title: "read file".into(),
            status: "in_progress".into(),
            raw_input: json!({"path": "/tmp/app.conf"}),
        }))
        .await;
        app.on_server(ServerMessage::Update(SessionUpdate::ToolCallUpdate {
            tool_call_id: "node-1".into(),
            status: "completed".into(),
            raw_output: Some(json!({"output": "port=8443"})),
        }))
        .await;
        assert!(app.transcript.iter().any(|c| matches!(
            c,
            Cell::Tool { title, status, args, output }
                if title == "read file"
                    && status == "completed"
                    && args.contains("path=/tmp/app.conf")
                    && output.as_deref() == Some("port=8443")
        )));
    }

    #[tokio::test]
    async fn node_meta_renders_tier_and_confidence() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::NodeMeta {
            node_type: "synthesis".into(),
            tier: Some(2),
            confidence: 0.78,
            flagged: true,
        }))
        .await;
        assert!(app.transcript.iter().any(
            |c| matches!(c, Cell::Meta(m) if m.contains("tier 2") && m.contains("0.78") && m.contains("flagged"))
        ));
    }

    #[tokio::test]
    async fn node_meta_for_result_node_is_suppressed() {
        let (mut app, _client, _rx) = app();
        let before = app.transcript.len();
        app.on_server(ServerMessage::Update(SessionUpdate::NodeMeta {
            node_type: "result".into(),
            tier: Some(0),
            confidence: 1.0,
            flagged: false,
        }))
        .await;
        assert_eq!(
            app.transcript.len(),
            before,
            "result-node meta is noise; skipped"
        );
    }

    #[tokio::test]
    async fn message_chunk_appends_a_message() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "the answer".into(),
            },
        }))
        .await;
        assert!(matches!(app.transcript.last(), Some(Cell::Message(m)) if m == "the answer"));
    }

    #[tokio::test]
    async fn turn_ended_returns_to_ready() {
        let (mut app, _client, _rx) = app();
        app.composer = "go".into();
        app.on_key(key(KeyCode::Enter)).await;
        assert_eq!(app.status, Status::Working);
        app.on_server(ServerMessage::TurnEnded("end_turn".into()))
            .await;
        assert_eq!(app.status, Status::Ready);
    }

    #[tokio::test]
    async fn esc_while_working_cancels_the_turn() {
        let (mut app, client, _rx) = app();
        app.composer = "go".into();
        app.on_key(key(KeyCode::Enter)).await; // now Working
        app.on_key(key(KeyCode::Esc)).await;
        let notes = client.notifications.lock().unwrap();
        assert_eq!(notes[0].0, "session/cancel");
        assert!(!app.should_quit);
    }

    #[tokio::test]
    async fn esc_when_idle_quits() {
        let (mut app, _client, _rx) = app();
        app.on_key(key(KeyCode::Esc)).await;
        assert!(app.should_quit);
    }

    #[tokio::test]
    async fn scroll_keys_adjust_scrollback() {
        let (mut app, _client, _rx) = app();
        app.on_key(key(KeyCode::PageUp)).await;
        assert_eq!(app.scrollback, 10);
        app.on_key(key(KeyCode::Up)).await;
        assert_eq!(app.scrollback, 11);
        app.on_key(key(KeyCode::Down)).await;
        assert_eq!(app.scrollback, 10);
        app.on_key(key(KeyCode::PageDown)).await;
        assert_eq!(app.scrollback, 0);
        // Saturates at the tail rather than underflowing.
        app.on_key(key(KeyCode::Down)).await;
        assert_eq!(app.scrollback, 0);
    }

    #[tokio::test]
    async fn spinner_advances_only_while_working() {
        let (mut app, _client, _rx) = app();
        app.tick_if_working();
        assert_eq!(app.spinner, 0, "idle: spinner is still");
        app.composer = "go".into();
        app.on_key(key(KeyCode::Enter)).await; // Working
        app.tick_if_working();
        assert_eq!(app.spinner, 1, "working: spinner advances");
    }

    #[tokio::test]
    async fn turn_end_reports_elapsed_time() {
        let (mut app, _client, _rx) = app();
        app.composer = "go".into();
        app.on_key(key(KeyCode::Enter)).await;
        assert!(app.turn_start.is_some());
        app.on_server(ServerMessage::TurnEnded("end_turn".into()))
            .await;
        assert!(app.turn_start.is_none());
        assert!(app
            .transcript
            .iter()
            .any(|c| matches!(c, Cell::Notice(n) if n.contains("end_turn") && n.contains('s'))));
    }

    #[tokio::test]
    async fn submitting_jumps_back_to_the_tail() {
        let (mut app, _client, _rx) = app();
        app.scrollback = 5;
        app.composer = "go".into();
        app.on_key(key(KeyCode::Enter)).await;
        assert_eq!(app.scrollback, 0);
    }
}
