//! App coordinator: holds transcript + UI state and reacts to terminal input
//! and agent messages. Rendering lives in `ui`; the wire lives in `acp`.

use std::collections::HashMap;
use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::acp::messages::{PermOption, PlanEntry, SessionUpdate, ToolCallPermission};
use crate::acp::{Outbound, ServerMessage};
use crate::graph::{self, GraphNode};
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
    tool_call: Option<ToolCallPermission>,
}

pub struct App<O: Outbound> {
    client: O,
    tx: mpsc::UnboundedSender<ServerMessage>,
    session_id: String,

    pub transcript: Vec<Cell>,
    pub plan: Vec<PlanEntry>,
    /// The plan's graph topology for the DAG view (empty until plan_graph).
    pub graph: Vec<GraphNode>,
    /// Whether the DAG view is open (toggled with Tab).
    pub show_dag: bool,
    /// Whether thought cells (think-node output) are expanded; toggled with `t`.
    pub thoughts_open: bool,
    pub composer: String,
    /// Cursor position in the composer, as a character index.
    pub cursor: usize,
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
    /// Submitted prompts, oldest first, for Ctrl-P/Ctrl-N recall.
    history: Vec<String>,
    /// Position while browsing history; `None` when editing a fresh line.
    history_pos: Option<usize>,
    /// True while consecutive AgentMessageChunks are accumulating into the
    /// last transcript cell. Any other update kind resets this to false so
    /// the next chunk starts a fresh cell.
    streaming_msg: bool,
    /// Identity of the current streaming message. Prevents adjacent chunks
    /// from different nodes (or final output) from merging into one cell.
    streaming_msg_key: Option<(Option<i64>, String, bool)>,
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
                "Type a task and press Enter. Tab: graph  t: thoughts  Esc: quit".into(),
            )],
            plan: Vec::new(),
            graph: Vec::new(),
            show_dag: false,
            thoughts_open: true,
            composer: String::new(),
            cursor: 0,
            status: Status::Ready,
            mode: Mode::Normal,
            should_quit: false,
            scrollback: 0,
            spinner: 0,
            turn_start: None,
            permission: None,
            working: false,
            tool_idx: HashMap::new(),
            history: Vec::new(),
            history_pos: None,
            streaming_msg: false,
            streaming_msg_key: None,
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

    pub fn permission_tool_call(&self) -> Option<&ToolCallPermission> {
        self.permission.as_ref().and_then(|p| p.tool_call.as_ref())
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
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
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
            KeyCode::Char('p') if ctrl => self.history_prev(),
            KeyCode::Char('n') if ctrl => self.history_next(),
            // Navigation shortcuts only fire when the composer is empty so
            // they don't swallow keystrokes mid-sentence.
            KeyCode::Char('t') if self.composer.is_empty() => {
                self.thoughts_open = !self.thoughts_open
            }
            // Ctrl-T works regardless of composer content (no conflict with typing).
            KeyCode::Char('t') if ctrl => self.thoughts_open = !self.thoughts_open,
            // g/G: jump transcript to top / tail (Vim convention).
            KeyCode::Char('g') if self.composer.is_empty() => self.scrollback = u16::MAX,
            KeyCode::Char('G') if self.composer.is_empty() => self.scrollback = 0,
            KeyCode::Char(c) => self.insert_char(c),
            KeyCode::Backspace => self.backspace(),
            KeyCode::Delete => self.delete(),
            KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right => self.cursor = (self.cursor + 1).min(self.char_count()),
            KeyCode::Home => self.cursor = 0,
            KeyCode::End => self.cursor = self.char_count(),
            KeyCode::Up => self.scroll(true, 1),
            KeyCode::Down => self.scroll(false, 1),
            KeyCode::PageUp => self.scroll(true, 10),
            KeyCode::PageDown => self.scroll(false, 10),
            KeyCode::Tab => self.show_dag = !self.show_dag,
            _ => {}
        }
    }

    // ── composer editing ─────────────────────────────────────────────────

    fn char_count(&self) -> usize {
        self.composer.chars().count()
    }

    /// Byte offset of the cursor's character index (end if past the last char).
    fn cursor_byte(&self) -> usize {
        self.composer
            .char_indices()
            .nth(self.cursor)
            .map(|(b, _)| b)
            .unwrap_or(self.composer.len())
    }

    fn insert_char(&mut self, c: char) {
        let at = self.cursor_byte();
        self.composer.insert(at, c);
        self.cursor += 1;
        self.history_pos = None;
    }

    fn backspace(&mut self) {
        if self.cursor == 0 {
            return;
        }
        self.cursor -= 1;
        let at = self.cursor_byte();
        self.composer.remove(at);
        self.history_pos = None;
    }

    fn delete(&mut self) {
        if self.cursor < self.char_count() {
            let at = self.cursor_byte();
            self.composer.remove(at);
            self.history_pos = None;
        }
    }

    fn set_composer(&mut self, text: String) {
        self.composer = text;
        self.cursor = self.char_count();
    }

    fn history_prev(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let pos = match self.history_pos {
            None => self.history.len() - 1,
            Some(p) => p.saturating_sub(1),
        };
        self.history_pos = Some(pos);
        self.set_composer(self.history[pos].clone());
    }

    fn history_next(&mut self) {
        let Some(pos) = self.history_pos else {
            return;
        };
        if pos + 1 < self.history.len() {
            self.history_pos = Some(pos + 1);
            self.set_composer(self.history[pos + 1].clone());
        } else {
            // Past the newest entry: back to an empty fresh line.
            self.history_pos = None;
            self.set_composer(String::new());
        }
    }

    async fn on_key_approval(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Char('a') => self.decide(approve_for(self), Status::Working).await,
            KeyCode::Char('A') => self.decide(always_allow_for(self), Status::Working).await,
            KeyCode::Char('r') => self.decide(reject_for(self, None), Status::Working).await,
            KeyCode::Char('e') if self.can_reject_with_feedback() => {
                self.mode = Mode::Feedback(String::new())
            }
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
                    self.decide(reject_for(self, Some(feedback)), Status::Working)
                        .await;
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
                    tool_call: params.tool_call,
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
        // Any update that is not a chunk closes the current streaming sequence
        // so the next chunk starts a fresh transcript cell.
        if !matches!(update, SessionUpdate::AgentMessageChunk { .. }) {
            self.streaming_msg = false;
            self.streaming_msg_key = None;
        }
        match update {
            SessionUpdate::Plan { entries } => {
                // Plan entries and graph nodes share the topological order, so
                // sync the graph's per-node status by position.
                for (node, entry) in self.graph.iter_mut().zip(&entries) {
                    node.status = entry.status.clone();
                }
                self.plan = entries;
            }
            SessionUpdate::PlanGraph { nodes } => self.graph = graph::build(nodes),
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
            SessionUpdate::AgentMessageChunk {
                content,
                node_id,
                node_type,
                is_final,
            } => {
                let is_thought = node_type == "think" && !is_final;
                let is_synthesis =
                    (node_type == "synthesis" || node_type == "summary") && !is_final;
                let key = (node_id, node_type, is_final);
                if self.streaming_msg
                    && self.streaming_msg_key.as_ref() == Some(&key)
                    && !content.text.is_empty()
                {
                    // Append any non-empty fragment — including whitespace-only
                    // ones — so fine-grained streaming preserves word boundaries.
                    // Fall back to a new cell if the last cell was replaced by
                    // something else (shouldn't happen, but be safe).
                    match self.transcript.last_mut() {
                        Some(Cell::Message(s))
                        | Some(Cell::Synthesis(s))
                        | Some(Cell::Thought(s)) => {
                            s.push_str(&content.text);
                            return;
                        }
                        _ => {}
                    }
                }
                // Not yet streaming: only open a new cell for visible content.
                if !content.text.trim().is_empty() {
                    if is_thought {
                        self.transcript.push(Cell::Thought(content.text));
                    } else if is_synthesis {
                        self.transcript.push(Cell::Synthesis(content.text));
                    } else {
                        self.transcript.push(Cell::Message(content.text));
                    }
                    self.streaming_msg = true;
                    self.streaming_msg_key = Some(key);
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
        self.cursor = 0;
        self.history.push(prompt.clone());
        self.history_pos = None;
        self.transcript.push(Cell::Prompt(prompt.clone()));
        self.plan.clear();
        self.graph.clear();
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

    fn permission_option(&self, candidates: &[&str], fallback: &str) -> String {
        self.permission
            .as_ref()
            .and_then(|p| {
                candidates.iter().find_map(|candidate| {
                    p.options
                        .iter()
                        .find(|opt| opt.option_id == *candidate)
                        .map(|opt| opt.option_id.clone())
                })
            })
            .unwrap_or_else(|| fallback.into())
    }

    fn can_reject_with_feedback(&self) -> bool {
        self.permission
            .as_ref()
            .is_some_and(|p| !p.plan.is_empty() && p.tool_call.is_none())
    }
}

fn selected(option_id: String) -> Value {
    json!({"outcome": {"outcome": "selected", "optionId": option_id}})
}

fn approve_for<O: Outbound>(app: &App<O>) -> Value {
    selected(app.permission_option(&["approve", "allow_once"], "approve"))
}

fn always_allow_for<O: Outbound>(app: &App<O>) -> Value {
    selected(app.permission_option(&["always_allow", "approve", "allow_once"], "approve"))
}

fn reject_for<O: Outbound>(app: &App<O>, feedback: Option<String>) -> Value {
    let option_id = app.permission_option(&["reject", "deny"], "reject");
    match feedback {
        Some(text) => json!({
            "outcome": {"outcome": "selected", "optionId": option_id},
            "feedback": text,
        }),
        None => selected(option_id),
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
                tool_call: None,
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
            node_id: None,
            node_type: String::new(),
            is_final: false,
        }))
        .await;
        assert!(matches!(app.transcript.last(), Some(Cell::Message(m)) if m == "the answer"));
    }

    #[tokio::test]
    async fn consecutive_chunks_accumulate_into_one_cell() {
        let (mut app, _client, _rx) = app();
        let len_before = app.transcript.len();
        for word in ["Hello", ", ", "world"] {
            app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
                content: Content { text: word.into() },
                node_id: Some(1),
                node_type: "think".into(),
                is_final: false,
            }))
            .await;
        }
        assert_eq!(
            app.transcript.len(),
            len_before + 1,
            "three consecutive chunks should produce one cell"
        );
        assert!(
            matches!(app.transcript.last(), Some(Cell::Thought(m)) if m == "Hello, world"),
            "cell text should be the concatenation of all chunks"
        );
    }

    #[tokio::test]
    async fn plan_update_between_chunks_starts_new_cell() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "first".into(),
            },
            node_id: Some(1),
            node_type: "think".into(),
            is_final: false,
        }))
        .await;
        // A plan update in between closes the streaming sequence.
        app.on_server(ServerMessage::Update(SessionUpdate::Plan {
            entries: vec![PlanEntry {
                content: "node".into(),
                status: "completed".into(),
            }],
        }))
        .await;
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "second".into(),
            },
            node_id: Some(2),
            node_type: "summary".into(),
            is_final: false,
        }))
        .await;
        let messages: Vec<_> = app
            .transcript
            .iter()
            .filter_map(|c| match c {
                Cell::Message(m) | Cell::Synthesis(m) | Cell::Thought(m) => Some(m.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(
            messages,
            vec!["first", "second"],
            "should be two separate cells"
        );
    }

    #[tokio::test]
    async fn different_node_chunks_start_new_cell() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "thinking".into(),
            },
            node_id: Some(1),
            node_type: "think".into(),
            is_final: false,
        }))
        .await;
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "final".into(),
            },
            node_id: None,
            node_type: "result".into(),
            is_final: true,
        }))
        .await;
        let messages: Vec<_> = app
            .transcript
            .iter()
            .filter_map(|c| match c {
                Cell::Message(m) => Some(m.as_str()),
                Cell::Thought(m) => Some(m.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(messages, vec!["thinking", "final"]);
        assert!(matches!(app.transcript[1], Cell::Thought(_)));
    }

    #[tokio::test]
    async fn whitespace_only_chunk_does_not_open_streaming_cell() {
        let (mut app, _client, _rx) = app();
        let len_before = app.transcript.len();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content { text: "  ".into() },
            node_id: Some(1),
            node_type: "think".into(),
            is_final: false,
        }))
        .await;
        assert_eq!(
            app.transcript.len(),
            len_before,
            "whitespace chunk is ignored"
        );
        assert!(!app.streaming_msg, "streaming_msg should not be set");
    }

    #[tokio::test]
    async fn whitespace_chunk_within_stream_preserves_word_boundaries() {
        let (mut app, _client, _rx) = app();
        for word in ["foo", " ", "bar"] {
            app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
                content: Content { text: word.into() },
                node_id: Some(1),
                node_type: "think".into(),
                is_final: false,
            }))
            .await;
        }
        assert!(
            matches!(app.transcript.last(), Some(Cell::Thought(m)) if m == "foo bar"),
            "space fragment must not be dropped while streaming"
        );
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
    async fn cursor_editing_inserts_and_deletes_in_place() {
        let (mut app, _client, _rx) = app();
        for c in "helo".chars() {
            app.on_key(key(KeyCode::Char(c))).await;
        }
        // Move left one and insert the missing 'l': "hel|o" -> "hell|o".
        app.on_key(key(KeyCode::Left)).await;
        app.on_key(key(KeyCode::Char('l'))).await;
        assert_eq!(app.composer, "hello");
        assert_eq!(app.cursor, 4);

        // Home, then Delete removes the first char.
        app.on_key(key(KeyCode::Home)).await;
        app.on_key(key(KeyCode::Delete)).await;
        assert_eq!(app.composer, "ello");
        assert_eq!(app.cursor, 0);

        // End, then Backspace removes the last char.
        app.on_key(key(KeyCode::End)).await;
        app.on_key(key(KeyCode::Backspace)).await;
        assert_eq!(app.composer, "ell");
        assert_eq!(app.cursor, 3);
    }

    fn ctrl(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::CONTROL)
    }

    #[tokio::test]
    async fn history_recall_with_ctrl_p_and_ctrl_n() {
        let (mut app, _client, _rx) = app();
        for prompt in ["first", "second"] {
            app.composer = prompt.into();
            app.cursor = app.char_count();
            app.on_key(key(KeyCode::Enter)).await;
            // End the turn so the next submit isn't blocked by `working`.
            app.on_server(ServerMessage::TurnEnded("end_turn".into()))
                .await;
        }
        // Ctrl-P walks back from newest to oldest.
        app.on_key(ctrl(KeyCode::Char('p'))).await;
        assert_eq!(app.composer, "second");
        assert_eq!(app.cursor, 6);
        app.on_key(ctrl(KeyCode::Char('p'))).await;
        assert_eq!(app.composer, "first");
        // Ctrl-N walks forward, then off the end to an empty line.
        app.on_key(ctrl(KeyCode::Char('n'))).await;
        assert_eq!(app.composer, "second");
        app.on_key(ctrl(KeyCode::Char('n'))).await;
        assert_eq!(app.composer, "");
    }

    #[tokio::test]
    async fn tab_toggles_the_dag_view() {
        let (mut app, _client, _rx) = app();
        assert!(!app.show_dag);
        app.on_key(key(KeyCode::Tab)).await;
        assert!(app.show_dag);
        app.on_key(key(KeyCode::Tab)).await;
        assert!(!app.show_dag);
    }

    #[tokio::test]
    async fn plan_graph_builds_layers_and_plan_updates_sync_status() {
        use crate::acp::messages::{GraphNode as WireNode, PlanEntry};
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::PlanGraph {
            nodes: vec![
                WireNode {
                    id: 1,
                    node_type: "tool".into(),
                    description: "read".into(),
                    deps: vec![],
                },
                WireNode {
                    id: 2,
                    node_type: "result".into(),
                    description: "answer".into(),
                    deps: vec![1],
                },
            ],
        }))
        .await;
        assert_eq!(app.graph.len(), 2);
        assert_eq!(app.graph[0].layer, 0);
        assert_eq!(app.graph[1].layer, 1);
        assert!(app.graph.iter().all(|n| n.status == "pending"));

        // A plan update syncs status onto the graph by position.
        app.on_server(ServerMessage::Update(SessionUpdate::Plan {
            entries: vec![
                PlanEntry {
                    content: "[tool] read".into(),
                    status: "completed".into(),
                },
                PlanEntry {
                    content: "[result] answer".into(),
                    status: "in_progress".into(),
                },
            ],
        }))
        .await;
        assert_eq!(app.graph[0].status, "completed");
        assert_eq!(app.graph[1].status, "in_progress");
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

    #[tokio::test]
    async fn t_key_toggles_thoughts_open() {
        let (mut app, _client, _rx) = app();
        assert!(app.thoughts_open, "thoughts visible by default");
        app.on_key(key(KeyCode::Char('t'))).await;
        assert!(!app.thoughts_open, "first press collapses");
        app.on_key(key(KeyCode::Char('t'))).await;
        assert!(app.thoughts_open, "second press re-expands");
    }

    #[tokio::test]
    async fn synthesis_chunks_land_in_synthesis_cell() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "combining results".into(),
            },
            node_id: Some(2),
            node_type: "synthesis".into(),
            is_final: false,
        }))
        .await;
        assert!(
            matches!(app.transcript.last(), Some(Cell::Synthesis(s)) if s == "combining results"),
            "non-final synthesis chunk should be a Synthesis cell"
        );
    }

    #[tokio::test]
    async fn summary_chunks_land_in_synthesis_cell() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "condensed".into(),
            },
            node_id: Some(3),
            node_type: "summary".into(),
            is_final: false,
        }))
        .await;
        assert!(
            matches!(app.transcript.last(), Some(Cell::Synthesis(_))),
            "non-final summary chunk should be a Synthesis cell"
        );
    }

    #[tokio::test]
    async fn final_result_chunk_lands_in_message_cell() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "final answer".into(),
            },
            node_id: Some(4),
            node_type: "result".into(),
            is_final: true,
        }))
        .await;
        assert!(
            matches!(app.transcript.last(), Some(Cell::Message(m)) if m == "final answer"),
            "final result chunk should be a Message cell"
        );
    }

    #[tokio::test]
    async fn thought_chunks_land_in_thought_cell() {
        let (mut app, _client, _rx) = app();
        app.on_server(ServerMessage::Update(SessionUpdate::AgentMessageChunk {
            content: Content {
                text: "step one".into(),
            },
            node_id: Some(1),
            node_type: "think".into(),
            is_final: false,
        }))
        .await;
        assert!(
            matches!(app.transcript.last(), Some(Cell::Thought(t)) if t == "step one"),
            "non-final think chunk should be a Thought cell"
        );
    }
}
