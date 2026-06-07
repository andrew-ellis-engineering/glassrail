//! glassrail-tui: a fast terminal client for the glassrail agent over ACP.
//!
//! Spawns `glassrail acp` as a subprocess, performs the ACP handshake, and runs
//! a ratatui loop that submits tasks, streams the plan and node execution, and
//! gates plan approval — all over JSON-RPC on the child's stdio.

mod acp;
mod app;
mod graph;
mod transcript;
mod ui;

use std::io::stdout;
use std::time::Duration;

use anyhow::{Context, Result};
use crossterm::event::{
    DisableMouseCapture, EnableMouseCapture, Event, EventStream, KeyEventKind, MouseEventKind,
};
use crossterm::execute;
use futures::StreamExt;
use serde_json::json;
use tokio::sync::mpsc;

use crate::acp::{AcpClient, Outbound, ServerMessage};
use crate::app::App;

#[tokio::main]
async fn main() -> Result<()> {
    let command = agent_command();
    let (tx, mut rx) = mpsc::unbounded_channel::<ServerMessage>();
    let (client, mut child) = AcpClient::spawn(&command, tx.clone())
        .with_context(|| format!("spawning agent: {}", command.join(" ")))?;

    let session_id = handshake(&client).await?;

    let mut terminal = ratatui::init();
    let _ = execute!(stdout(), EnableMouseCapture);
    let mut app = App::new(client, tx, session_id);
    let result = run(&mut terminal, &mut app, &mut rx).await;
    let _ = execute!(stdout(), DisableMouseCapture);
    ratatui::restore();

    let _ = child.start_kill();
    result
}

/// initialize + session/new; returns the new session id.
async fn handshake(client: &AcpClient) -> Result<String> {
    client
        .request(
            "initialize",
            json!({"protocolVersion": 1, "clientCapabilities": {}}),
        )
        .await
        .context("initialize failed")?;

    let cwd = std::env::current_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    let session = client
        .request("session/new", json!({"cwd": cwd, "mcpServers": []}))
        .await
        .context("session/new failed")?;
    session
        .get("sessionId")
        .and_then(|s| s.as_str())
        .map(str::to_string)
        .context("session/new returned no sessionId")
}

async fn run<O: Outbound>(
    terminal: &mut ratatui::DefaultTerminal,
    app: &mut App<O>,
    rx: &mut mpsc::UnboundedReceiver<ServerMessage>,
) -> Result<()> {
    let mut events = EventStream::new();
    // A steady tick animates the spinner and keeps the elapsed timer live.
    let mut ticker = tokio::time::interval(Duration::from_millis(100));
    loop {
        terminal.draw(|frame| ui::render(frame, app))?;
        if app.should_quit {
            break;
        }
        tokio::select! {
            maybe_event = events.next() => match maybe_event {
                Some(Ok(Event::Key(key))) if key.kind == KeyEventKind::Press => app.on_key(key).await,
                Some(Ok(Event::Mouse(m))) => match m.kind {
                    MouseEventKind::ScrollUp => app.scroll(true, 3),
                    MouseEventKind::ScrollDown => app.scroll(false, 3),
                    _ => {}
                },
                Some(Ok(_)) => {}
                Some(Err(_)) | None => app.should_quit = true,
            },
            msg = rx.recv() => match msg {
                Some(message) => app.on_server(message).await,
                None => app.should_quit = true,
            },
            _ = ticker.tick() => app.tick_if_working(),
        }
    }
    Ok(())
}

/// Resolve the agent command: positional args, then `GLASSRAIL_AGENT_CMD`, then
/// the default `glassrail acp`. Mirrors the eval framework's configurable backend
/// (the agent need not be on PATH — e.g. `glassrail-tui uv run glassrail acp`).
fn agent_command() -> Vec<String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if !args.is_empty() {
        return args;
    }
    if let Ok(cmd) = std::env::var("GLASSRAIL_AGENT_CMD") {
        let parts: Vec<String> = cmd.split_whitespace().map(String::from).collect();
        if !parts.is_empty() {
            return parts;
        }
    }
    vec!["glassrail".into(), "acp".into()]
}
