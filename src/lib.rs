//! ieodo — a local record of every AI conversation (user words and
//! final answers) and the history of the Markdown documents they touch,
//! shared by every AI on this machine through MCP.

pub mod http;
pub mod mcp;
pub mod records;
pub mod setup;
pub mod stdio;
pub mod text;

use std::path::PathBuf;

/// `IEODO_HOME`, or `~/.ieodo`.
pub fn data_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("IEODO_HOME")
        && !dir.trim().is_empty()
    {
        return PathBuf::from(dir);
    }
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".ieodo")
}

/// The record store (wiki/schema.md). The previous store, `groundlink.db`,
/// is left on disk untouched.
pub fn records_path() -> PathBuf {
    data_dir().join("records.db")
}
