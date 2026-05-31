//! ratatui rendering for the app: status line, live plan, transcript, composer,
//! and the modal approval / feedback overlay.

use ratatui::layout::{Alignment, Constraint, Flex, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::acp::messages::{PermOption, PlanEntry};
use crate::acp::Outbound;
use crate::app::{App, Mode, Status, SPINNER};
use crate::transcript::Cell;

pub fn render<O: Outbound>(frame: &mut Frame, app: &App<O>) {
    let chunks = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(3),
    ])
    .split(frame.area());

    let spinner = SPINNER[app.spinner % SPINNER.len()];
    let elapsed = app.turn_start.map(|t| t.elapsed().as_secs());
    render_status(frame, chunks[0], app.status, spinner, elapsed);
    render_body(frame, chunks[1], &app.plan, &app.transcript, app.scrollback);
    render_composer(frame, chunks[2], &app.composer);

    match &app.mode {
        Mode::Approval => render_approval(
            frame,
            app.permission_plan().unwrap_or(&[]),
            app.permission_options().unwrap_or(&[]),
        ),
        Mode::Feedback(buf) => render_feedback(frame, buf),
        Mode::Normal => {}
    }
}

fn render_status(
    frame: &mut Frame,
    area: Rect,
    status: Status,
    spinner: &str,
    elapsed: Option<u64>,
) {
    let (label, color) = match status {
        Status::Ready => ("● ready".to_string(), Color::Green),
        Status::Working => {
            let secs = elapsed.unwrap_or(0);
            (format!("{spinner} working… {secs}s"), Color::Yellow)
        }
        Status::AwaitingApproval => ("⏸ awaiting approval".to_string(), Color::Magenta),
    };
    let line = Line::from(vec![
        Span::styled(
            " dagagent ",
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("  "),
        Span::styled(label, Style::default().fg(color)),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

fn render_body(
    frame: &mut Frame,
    area: Rect,
    plan: &[PlanEntry],
    transcript: &[Cell],
    scrollback: u16,
) {
    if plan.is_empty() {
        render_transcript(frame, area, transcript, scrollback);
        return;
    }
    let plan_h = (plan.len() as u16 + 2).min(area.height / 2).max(3);
    let parts = Layout::vertical([Constraint::Length(plan_h), Constraint::Min(1)]).split(area);
    render_plan(frame, parts[0], plan);
    render_transcript(frame, parts[1], transcript, scrollback);
}

fn render_plan(frame: &mut Frame, area: Rect, plan: &[PlanEntry]) {
    let lines: Vec<Line> = plan.iter().map(plan_line).collect();
    let block = Block::default().borders(Borders::ALL).title(" plan ");
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn plan_line(entry: &PlanEntry) -> Line<'static> {
    let (glyph, color) = match entry.status.as_str() {
        "completed" => ("✔", Color::Green),
        "in_progress" => ("▶", Color::Yellow),
        _ => ("·", Color::DarkGray),
    };
    Line::from(vec![
        Span::styled(format!("{glyph} "), Style::default().fg(color)),
        Span::raw(entry.content.clone()),
    ])
}

fn render_transcript(frame: &mut Frame, area: Rect, transcript: &[Cell], scrollback: u16) {
    let mut lines: Vec<Line> = Vec::new();
    for cell in transcript {
        match cell {
            Cell::Prompt(text) => lines.push(Line::from(Span::styled(
                format!("❯ {text}"),
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ))),
            Cell::Message(text) => {
                for raw in text.split('\n') {
                    lines.push(Line::raw(raw.to_string()));
                }
            }
            Cell::Tool {
                title,
                args,
                status,
                output,
            } => {
                let mut spans = vec![
                    Span::styled("⚙ ", Style::default().fg(Color::Blue)),
                    Span::raw(title.clone()),
                ];
                if !args.is_empty() {
                    spans.push(Span::styled(
                        format!("  ({args})"),
                        Style::default().fg(Color::DarkGray),
                    ));
                }
                spans.push(Span::styled(
                    format!("  [{status}]"),
                    Style::default().fg(Color::DarkGray),
                ));
                lines.push(Line::from(spans));
                if let Some(out) = output {
                    lines.push(Line::from(Span::styled(
                        format!("  ↳ {out}"),
                        Style::default().fg(Color::DarkGray),
                    )));
                }
            }
            Cell::Meta(text) => lines.push(Line::from(Span::styled(
                format!("  {text}"),
                Style::default()
                    .fg(Color::DarkGray)
                    .add_modifier(Modifier::DIM),
            ))),
            Cell::Notice(text) => lines.push(Line::from(Span::styled(
                text.clone(),
                Style::default()
                    .fg(Color::DarkGray)
                    .add_modifier(Modifier::ITALIC),
            ))),
        }
        lines.push(Line::raw(""));
    }

    // Title hints when scrolled up from the tail.
    let title = if scrollback > 0 {
        " transcript (scrolled — ↓/PgDn for latest) ".to_string()
    } else {
        " transcript ".to_string()
    };
    let block = Block::default().borders(Borders::ALL).title(title);
    let inner_h = area.height.saturating_sub(2);
    // Pin to the tail, then let scrollback move the window up (clamped at the top).
    let max_scroll = (lines.len() as u16).saturating_sub(inner_h);
    let scroll = max_scroll.saturating_sub(scrollback);
    let para = Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .scroll((scroll, 0));
    frame.render_widget(para, area);
}

fn render_composer(frame: &mut Frame, area: Rect, composer: &str) {
    let block = Block::default().borders(Borders::ALL).title(" task ");
    let body = Line::from(vec![
        Span::styled("> ", Style::default().fg(Color::Cyan)),
        Span::raw(composer.to_string()),
        Span::styled("▏", Style::default().fg(Color::Cyan)),
    ]);
    frame.render_widget(Paragraph::new(body).block(block), area);
}

fn render_approval(frame: &mut Frame, plan: &[PlanEntry], options: &[PermOption]) {
    let mut lines: Vec<Line> = vec![Line::from(Span::styled(
        "Review the plan, then choose:",
        Style::default().add_modifier(Modifier::BOLD),
    ))];
    for entry in plan {
        lines.push(plan_line(entry));
    }
    if !options.is_empty() {
        lines.push(Line::raw(""));
        for opt in options {
            lines.push(Line::from(Span::styled(
                format!("  • {} ({})", opt.name, opt.option_id),
                Style::default().fg(Color::DarkGray),
            )));
        }
    }
    lines.push(Line::raw(""));
    lines.push(Line::from(Span::styled(
        "[a] approve   [e] reject with feedback   [r] reject   [esc] cancel",
        Style::default().fg(Color::Yellow),
    )));

    let height = (lines.len() as u16 + 2).min(frame.area().height.saturating_sub(2));
    let area = centered(frame.area(), 70, height);
    frame.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" plan approval ")
        .border_style(Style::default().fg(Color::Magenta));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn render_feedback(frame: &mut Frame, buf: &str) {
    let lines = vec![
        Line::from(Span::styled(
            "Feedback for the revised plan (Enter to send, Esc to go back):",
            Style::default().add_modifier(Modifier::BOLD),
        )),
        Line::raw(""),
        Line::from(vec![
            Span::styled("> ", Style::default().fg(Color::Cyan)),
            Span::raw(buf.to_string()),
            Span::styled("▏", Style::default().fg(Color::Cyan)),
        ]),
    ];
    let area = centered(frame.area(), 70, 5);
    frame.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" revise ")
        .border_style(Style::default().fg(Color::Magenta));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .alignment(Alignment::Left)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn centered(area: Rect, percent_x: u16, height: u16) -> Rect {
    let h = Layout::horizontal([Constraint::Percentage(percent_x)])
        .flex(Flex::Center)
        .split(area);
    Layout::vertical([Constraint::Length(height)])
        .flex(Flex::Center)
        .split(h[0])[0]
}
