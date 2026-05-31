//! A canned ACP agent for driving the TUI without a model server.
//!
//! Speaks just enough of the protocol over stdio to exercise the client end to
//! end: handshake, a plan that streams node-by-node, a plan-approval gate (with
//! guided replan on reject-with-feedback), and a final result. No dagagent, no
//! MLX — purely scripted, with small delays so the streaming is visible.
//!
//! Run the TUI against it (the client spawns this as its agent):
//!
//! ```text
//! cargo run -- cargo run --quiet --example fake_agent
//! ```

use std::io::{self, BufRead, Write};
use std::thread::sleep;
use std::time::Duration;

use serde_json::{json, Value};

fn send(value: &Value) {
    let mut out = io::stdout();
    writeln!(out, "{value}").expect("write stdout");
    out.flush().expect("flush stdout");
}

fn update(session: &str, update: Value) {
    send(&json!({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session, "update": update},
    }));
    sleep(Duration::from_millis(350));
}

fn node_meta(session: &str, node_type: &str, tier: u32, confidence: f64, flagged: bool) {
    update(
        session,
        json!({
            "sessionUpdate": "node_meta",
            "nodeType": node_type,
            "tier": tier,
            "confidence": confidence,
            "flagged": flagged,
        }),
    );
}

fn plan(statuses: [&str; 3]) -> Value {
    let titles = ["read the brief", "analyse the options", "write the answer"];
    let entries: Vec<Value> = titles
        .iter()
        .zip(statuses)
        .map(
            |(content, status)| json!({"content": content, "priority": "medium", "status": status}),
        )
        .collect();
    json!({"sessionUpdate": "plan", "entries": entries})
}

fn read_msg(reader: &mut impl BufRead) -> Option<Value> {
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).ok()? == 0 {
            return None; // stdin closed
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        return serde_json::from_str(trimmed).ok();
    }
}

/// Run one prompt turn: gate, then stream to a result (or stop on rejection).
fn handle_prompt(session: &str, reply_id: &Value, reader: &mut impl BufRead) {
    // Announce the plan and ask for approval.
    update(session, plan(["pending", "pending", "pending"]));
    send(&json!({
        "jsonrpc": "2.0",
        "id": 9001,
        "method": "session/request_permission",
        "params": {
            "sessionId": session,
            "plan": {"entries": plan(["pending", "pending", "pending"])["entries"].clone()},
            "options": [
                {"optionId": "approve", "name": "Approve and run this plan", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject the plan", "kind": "reject_once"},
            ],
        },
    }));

    let decision = read_msg(reader);
    let (choice, feedback) = parse_decision(decision.as_ref());

    match choice.as_deref() {
        Some("approve") => stream_result(session, reply_id),
        Some("reject") if feedback.is_some() => {
            // Guided replan: re-gate, and approve-or-not on the next round.
            update(
                session,
                json!({
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": format!("Revising per feedback: {}", feedback.unwrap())},
                }),
            );
            handle_prompt(session, reply_id, reader);
        }
        Some("reject") => respond_stop(reply_id, "refusal"),
        _ => respond_stop(reply_id, "cancelled"),
    }
}

fn stream_result(session: &str, reply_id: &Value) {
    update(session, plan(["in_progress", "pending", "pending"]));
    update(
        session,
        json!({
            "sessionUpdate": "tool_call",
            "toolCallId": "node-1",
            "title": "read the brief",
            "kind": "read",
            "status": "in_progress",
            "rawInput": {"path": "brief.md"},
        }),
    );
    update(
        session,
        json!({
            "sessionUpdate": "tool_call_update",
            "toolCallId": "node-1",
            "status": "completed",
            "rawOutput": {"output": "Budget: $42k. Deadline: Q3. Owner: Elena Voss."},
        }),
    );
    node_meta(session, "tool", 0, 1.0, false);
    update(session, plan(["completed", "in_progress", "pending"]));
    update(
        session,
        json!({
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Weighing the trade-offs of the three options…"},
        }),
    );
    node_meta(session, "synthesis", 2, 0.74, true);
    update(session, plan(["completed", "completed", "in_progress"]));
    update(
        session,
        json!({
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Recommendation: go with option B — best fit for the stated constraints."},
        }),
    );
    update(session, plan(["completed", "completed", "completed"]));
    respond_stop(reply_id, "end_turn");
}

fn parse_decision(msg: Option<&Value>) -> (Option<String>, Option<String>) {
    let Some(msg) = msg else {
        return (None, None);
    };
    let result = msg.get("result").unwrap_or(msg);
    let outcome = result.get("outcome");
    let inner = outcome
        .and_then(|o| o.get("outcome"))
        .and_then(Value::as_str);
    if inner != Some("selected") {
        return (None, None);
    }
    let option = outcome
        .and_then(|o| o.get("optionId"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let feedback = result
        .get("feedback")
        .or_else(|| outcome.and_then(|o| o.get("feedback")))
        .and_then(Value::as_str)
        .filter(|f| !f.trim().is_empty())
        .map(str::to_string);
    (option, feedback)
}

fn respond_stop(reply_id: &Value, reason: &str) {
    send(&json!({"jsonrpc": "2.0", "id": reply_id, "result": {"stopReason": reason}}));
}

fn main() {
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let session = "fake-session";
    while let Some(msg) = read_msg(&mut reader) {
        let method = msg.get("method").and_then(Value::as_str);
        let id = msg.get("id").cloned().unwrap_or(Value::Null);
        match method {
            Some("initialize") => send(&json!({
                "jsonrpc": "2.0",
                "id": id,
                "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": false}},
            })),
            Some("session/new") => {
                send(&json!({"jsonrpc": "2.0", "id": id, "result": {"sessionId": session}}))
            }
            Some("session/prompt") => handle_prompt(session, &id, &mut reader),
            _ => {} // session/cancel and anything else: ignore
        }
    }
}
