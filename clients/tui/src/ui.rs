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
use crate::graph::GraphNode;
use crate::transcript::Cell;

pub fn render<O: Outbound>(frame: &mut Frame, app: &App<O>) {
    // Minimum size gate: anything smaller can't render a usable layout.
    if frame.area().width < 40 || frame.area().height < 8 {
        let msg = Paragraph::new("Terminal too small — resize to at least 40×8")
            .alignment(Alignment::Center)
            .style(Style::default().fg(Color::Yellow));
        frame.render_widget(Clear, frame.area());
        frame.render_widget(msg, frame.area());
        return;
    }

    let composer_h = composer_height(&app.composer, frame.area().width);
    let chunks = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(composer_h),
    ])
    .split(frame.area());

    let spinner = SPINNER[app.spinner % SPINNER.len()];
    let elapsed = app.turn_start.map(|t| t.elapsed().as_secs());
    render_status(frame, chunks[0], app.status, spinner, elapsed);
    if app.show_dag {
        render_dag(frame, chunks[1], &app.graph);
    } else {
        render_body(
            frame,
            chunks[1],
            &app.plan,
            &app.transcript,
            app.scrollback,
            app.thoughts_open,
        );
    }
    render_composer(frame, chunks[2], &app.composer, app.cursor);

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

    // Context-sensitive hints shown right-aligned so they don't burn the full line.
    let hints: &str = match status {
        Status::Ready => "Tab:graph  t:thoughts  g:top  G:tail  ?:keys",
        Status::Working => "Esc:cancel",
        Status::AwaitingApproval => "a:approve  r:reject  e:reject+feedback",
    };
    let hint_w = hints.chars().count() as u16 + 1; // +1 for right margin

    let left_line = Line::from(vec![
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
    let right_line = Line::from(Span::styled(
        format!("{hints} "),
        Style::default().add_modifier(Modifier::DIM),
    ));

    // Only show hints if there's enough room (left brand+status takes ~20 chars).
    if area.width > hint_w + 20 {
        let parts =
            Layout::horizontal([Constraint::Min(1), Constraint::Length(hint_w)]).split(area);
        frame.render_widget(Paragraph::new(left_line), parts[0]);
        frame.render_widget(Paragraph::new(right_line), parts[1]);
    } else {
        frame.render_widget(Paragraph::new(left_line), area);
    }
}

fn render_body(
    frame: &mut Frame,
    area: Rect,
    plan: &[PlanEntry],
    transcript: &[Cell],
    scrollback: u16,
    thoughts_open: bool,
) {
    if plan.is_empty() {
        render_transcript(frame, area, transcript, scrollback, thoughts_open);
        return;
    }
    let plan_h = (plan.len() as u16 + 2).min(area.height / 2).max(3);
    let parts = Layout::vertical([Constraint::Length(plan_h), Constraint::Min(1)]).split(area);
    render_plan(frame, parts[0], plan);
    render_transcript(frame, parts[1], transcript, scrollback, thoughts_open);
}

fn render_plan(frame: &mut Frame, area: Rect, plan: &[PlanEntry]) {
    let lines: Vec<Line> = plan.iter().map(plan_line).collect();
    let block = Block::default().borders(Borders::ALL).title(" plan ");
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn status_glyph(status: &str) -> (&'static str, Color) {
    match status {
        "completed" => ("✔", Color::Green),
        "in_progress" => ("▶", Color::Yellow),
        "failed" => ("✗", Color::Red),
        _ => ("·", Color::DarkGray),
    }
}

fn plan_line(entry: &PlanEntry) -> Line<'static> {
    let (glyph, color) = status_glyph(&entry.status);
    Line::from(vec![
        Span::styled(format!("{glyph} "), Style::default().fg(color)),
        Span::raw(entry.content.clone()),
    ])
}

/// The DAG view: nodes grouped into topological layers (parallel cohorts),
/// each coloured by live status. Edges/connectors are not drawn yet.
fn render_dag(frame: &mut Frame, area: Rect, graph: &[GraphNode]) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" graph — Tab to close ");
    let mut lines: Vec<Line> = Vec::new();
    if graph.is_empty() {
        lines.push(Line::from(Span::styled(
            "no plan yet — submit a task",
            Style::default()
                .fg(Color::DarkGray)
                .add_modifier(Modifier::ITALIC),
        )));
    } else {
        for layer in 0..=crate::graph::max_layer(graph) {
            lines.push(Line::from(Span::styled(
                format!("layer {layer}"),
                Style::default()
                    .fg(Color::DarkGray)
                    .add_modifier(Modifier::BOLD),
            )));
            for node in graph.iter().filter(|n| n.layer == layer) {
                let (glyph, color) = status_glyph(&node.status);
                lines.push(Line::from(vec![
                    Span::raw("   "),
                    Span::styled(format!("{glyph} "), Style::default().fg(color)),
                    Span::styled(
                        format!("#{} [{}] ", node.id, node.node_type),
                        Style::default().fg(Color::DarkGray),
                    ),
                    Span::raw(node.description.clone()),
                ]));
            }
            lines.push(Line::raw(""));
        }
    }
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

/// Dim style for secondary/muted content. Uses the terminal's default
/// foreground + DIM rather than a hard-coded DarkGray, so it degrades
/// gracefully on both dark and light terminal themes.
const MUTED: Style = Style::new().add_modifier(Modifier::DIM);

fn render_transcript(
    frame: &mut Frame,
    area: Rect,
    transcript: &[Cell],
    scrollback: u16,
    thoughts_open: bool,
) {
    let inner_w = area.width.saturating_sub(2).max(1) as usize;
    let mut lines: Vec<Line> = Vec::new();

    // Track whether the previous cell was a Thought so collapsed runs are
    // emitted once per contiguous group rather than once globally.
    let mut in_thought_run = false;

    for cell in transcript {
        // Any non-Thought cell ends the current thought run.
        if !matches!(cell, Cell::Thought(_)) {
            in_thought_run = false;
        }

        match cell {
            Cell::Prompt(text) => lines.extend(wrap_styled(
                &format!("❯ {text}"),
                inner_w,
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            )),
            Cell::Message(text) => {
                // Final answer: highest visual weight. Bold + green bullet
                // so the eye can jump straight to the answer on scrollback.
                for (i, raw) in text.split('\n').enumerate() {
                    let prefix = if i == 0 { "● " } else { "  " };
                    lines.extend(wrap_styled(
                        &format!("{prefix}{raw}"),
                        inner_w,
                        Style::default().add_modifier(Modifier::BOLD),
                    ));
                }
            }
            Cell::Synthesis(text) => {
                // Intermediate synthesis/summary: clearly secondary, but not
                // as faded as thoughts. Magenta avoids the blue used by tools.
                for raw in text.split('\n') {
                    lines.extend(wrap_styled(
                        &format!("↻ {raw}"),
                        inner_w,
                        Style::default().fg(Color::Magenta),
                    ));
                }
            }
            Cell::Thought(text) => {
                if thoughts_open {
                    for raw in text.split('\n') {
                        lines.extend(wrap_styled(
                            &format!("> {raw}"),
                            inner_w,
                            MUTED.add_modifier(Modifier::ITALIC),
                        ));
                    }
                } else {
                    // Collapsed: emit one header per contiguous run of Thought
                    // cells, not one header for the entire transcript.
                    if !in_thought_run {
                        lines.push(Line::from(Span::styled(
                            "⟩ thinking  (t to expand)",
                            MUTED.add_modifier(Modifier::ITALIC),
                        )));
                        in_thought_run = true;
                    }
                    // Skip the blank separator for collapsed thoughts.
                    continue;
                }
            }
            Cell::Tool {
                title,
                args,
                status,
                output,
            } => {
                let mut spans = vec![
                    Span::styled("⚙ ", Style::default().fg(Color::Cyan)),
                    Span::raw(title.clone()),
                ];
                if !args.is_empty() {
                    spans.push(Span::styled(format!("  ({args})"), MUTED));
                }
                spans.push(Span::styled(format!("  [{status}]"), MUTED));
                lines.extend(wrap_line(Line::from(spans), inner_w));
                if let Some(out) = output {
                    lines.extend(wrap_styled(&format!("  ↳ {out}"), inner_w, MUTED));
                }
            }
            Cell::Meta(text) => {
                lines.extend(wrap_styled(&format!("  {text}"), inner_w, MUTED));
            }
            Cell::Notice(text) => {
                lines.extend(wrap_styled(
                    text,
                    inner_w,
                    MUTED.add_modifier(Modifier::ITALIC),
                ));
            }
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
    let para = Paragraph::new(lines).block(block).scroll((scroll, 0));
    frame.render_widget(para, area);
}

fn render_composer(frame: &mut Frame, area: Rect, composer: &str, cursor: usize) {
    let block = Block::default().borders(Borders::ALL).title(" task ");
    let inner_w = area.width.saturating_sub(2).max(1) as usize;
    let chars: Vec<char> = composer.chars().collect();
    let at = cursor.min(chars.len());
    let cursor_style = Style::default().add_modifier(Modifier::REVERSED);

    let mut cells = vec![
        Span::styled(">".to_string(), Style::default().fg(Color::Cyan)),
        Span::raw(" ".to_string()),
    ];
    for (idx, ch) in chars.iter().enumerate() {
        let style = if idx == at {
            cursor_style
        } else {
            Style::default()
        };
        cells.push(Span::styled(ch.to_string(), style));
    }
    if at == chars.len() {
        cells.push(Span::styled(" ".to_string(), cursor_style));
    }

    let mut lines = chunk_spans(cells, inner_w);
    let visible_h = area.height.saturating_sub(2).max(1) as usize;
    if lines.len() > visible_h {
        lines = lines.split_off(lines.len() - visible_h);
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn composer_height(composer: &str, terminal_w: u16) -> u16 {
    let inner_w = terminal_w.saturating_sub(2).max(1) as usize;
    let chars = composer.chars().count() + 3; // prompt marker, space, cursor block
    let visual = chars.div_ceil(inner_w).max(1) as u16;
    (visual + 2).clamp(3, 8)
}

fn wrap_styled(text: &str, width: usize, style: Style) -> Vec<Line<'static>> {
    wrap_plain(text, width)
        .into_iter()
        .map(|line| Line::from(Span::styled(line, style)))
        .collect()
}

fn wrap_line(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    let spans: Vec<Span<'static>> = line
        .spans
        .into_iter()
        .flat_map(|span| {
            let style = span.style;
            span.content
                .chars()
                .map(move |ch| Span::styled(ch.to_string(), style))
                .collect::<Vec<_>>()
        })
        .collect();
    chunk_spans(spans, width)
}

fn chunk_spans(spans: Vec<Span<'static>>, width: usize) -> Vec<Line<'static>> {
    let width = width.max(1);
    if spans.is_empty() {
        return vec![Line::raw("")];
    }
    let mut lines = Vec::new();
    for chunk in spans.chunks(width) {
        lines.push(Line::from(chunk.to_vec()));
    }
    lines
}

/// Word-aware line wrapper. Splits `text` into segments of at most `width`
/// characters, breaking at whitespace where possible and hard-breaking any
/// single word that still overflows.  The output always contains at least one
/// element; an empty input yields `[""]`.
fn wrap_plain(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    if text.is_empty() {
        return vec![String::new()];
    }
    let mut lines: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut col = 0usize;
    for word in text.split_ascii_whitespace() {
        let w = word.chars().count();
        if col == 0 {
            current.push_str(word);
            col = w;
        } else if col + 1 + w <= width {
            current.push(' ');
            current.push_str(word);
            col += 1 + w;
        } else {
            lines.push(std::mem::take(&mut current));
            current.push_str(word);
            col = w;
        }
        // Hard-break any word that overflows the line width.
        while col > width {
            let bp = current
                .char_indices()
                .nth(width)
                .map(|(b, _)| b)
                .unwrap_or(current.len());
            let rest = current.split_off(bp);
            lines.push(std::mem::replace(&mut current, rest));
            col = current.chars().count();
        }
    }
    lines.push(current);
    lines
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transcript_wrapping_counts_visual_lines() {
        // A single long word with no spaces hard-breaks at the width boundary.
        let lines = wrap_styled("abcdef", 2, Style::default());
        let rendered: Vec<String> = lines
            .into_iter()
            .map(|line| line.spans.into_iter().map(|span| span.content).collect())
            .collect();
        assert_eq!(rendered, vec!["ab", "cd", "ef"]);
    }

    #[test]
    fn wrap_plain_breaks_at_word_boundaries() {
        // "hello world" at width 7: "hello" fits (5), " world" would be 11 — wrap.
        let lines = wrap_plain("hello world", 7);
        assert_eq!(lines, vec!["hello", "world"]);
    }

    #[test]
    fn wrap_plain_fits_short_text_on_one_line() {
        let lines = wrap_plain("hi", 80);
        assert_eq!(lines, vec!["hi"]);
    }

    #[test]
    fn wrap_plain_hard_breaks_overlong_words() {
        let lines = wrap_plain("abcdefgh", 3);
        assert_eq!(lines, vec!["abc", "def", "gh"]);
    }

    #[test]
    fn wrap_plain_empty_is_one_empty_string() {
        assert_eq!(wrap_plain("", 80), vec![""]);
    }

    #[test]
    fn wrap_plain_multiple_words_pack_greedily() {
        // "one two" fits on width 7 ("one two" = 7 chars exactly).
        let lines = wrap_plain("one two three", 7);
        assert_eq!(lines, vec!["one two", "three"]);
    }

    #[test]
    fn composer_height_grows_for_wrapped_input() {
        assert_eq!(composer_height("abc", 10), 3);
        assert_eq!(composer_height("abcdefghij", 6), 6);
        assert_eq!(composer_height("abcdefghijklmnopqrstuvwxyz", 6), 8);
    }

    #[test]
    fn wrapped_composer_keeps_cursor_cell() {
        let lines = chunk_spans(
            vec![
                Span::raw("a".to_string()),
                Span::styled(
                    "b".to_string(),
                    Style::default().add_modifier(Modifier::REVERSED),
                ),
                Span::raw("c".to_string()),
            ],
            2,
        );
        assert_eq!(lines.len(), 2);
        assert!(lines[0].spans[1]
            .style
            .add_modifier
            .contains(Modifier::REVERSED));
    }
}
