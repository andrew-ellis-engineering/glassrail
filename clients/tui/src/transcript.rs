//! The conversation transcript: an ordered list of rendered cells.
//!
//! The live plan is held separately (the agent re-sends it whole on each
//! change, so we replace rather than append); everything else accumulates here
//! in arrival order.

/// One rendered item in the transcript.
#[derive(Debug, Clone)]
pub enum Cell {
    /// A prompt the user submitted.
    Prompt(String),
    /// An agent message chunk (think/summary/synthesis output, or the result).
    Message(String),
    /// A tool call: its title, arguments, latest status, and (once done) output.
    Tool {
        title: String,
        args: String,
        status: String,
        output: Option<String>,
    },
    /// A dim per-node metadata annotation (tier / confidence / flagged).
    Meta(String),
    /// A status / branch / error notice.
    Notice(String),
}
