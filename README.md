# ieodo (이어도)

English | [한국어](README.ko.md)

**About the name:** *ieodo* (이어도) is how you say *이어줘* — "connect it for me" — in the Gyeongsang dialect of Korean. That is the job: it connects one AI conversation to the next, so the context carries over.

*Formerly groundlink. Upgrading from it: see [CHANGELOG.md](CHANGELOG.md).*

A local MCP server that automatically records the **conversations** you have with AI coding tools (Claude Code, Codex, Antigravity) on one computer, and the **history of the documents** those conversations change. Capture hooks write; AIs only read. Design notes (Korean): [wiki/schema.md](wiki/schema.md).

## Why
AI coding tools forget a conversation when the session ends. The next session does not know why something was decided yesterday or how far the work got, so you explain it again. With several tools it gets worse: Claude Code does not know what you settled in Codex, and nobody remembers why a document was changed in Antigravity.

ieodo keeps those conversations and document changes in one place, automatically. Any AI can look them up through MCP tools. You never save or summarize anything by hand.

## In 30 seconds
- **Automatic capture**: in each registered project, hooks store every turn (your words + the final answer, with the model that answered) and every change to `.md` files.
- **AIs only read**: three tools, `search` (full-text), `recent` (latest turns), `history` (one document's changes).
- **Local only**: the server listens on `127.0.0.1`; everything lives in one file, `~/.ieodo/records.db`. Known secret formats are redacted before storing.
- **Append-only**: database triggers block edits and deletes. A record shows what happened, not that it was right.

## How it is used (example)
In a new Claude Code session you ask:

> Last time, what cache expiry did we settle on with Codex?

The AI calls ieodo's `search`:

```json
{"query": "cache expiry", "project": "my-app"}
```

Each hit says which session and turn it came from and which model answered:

```json
{"hits": [{"kind": "turn", "session_id": "codex:019a…", "turn_no": 7,
  "excerpt": "…keep the cache expiry at 15 minutes and clear it right after login…",
  "models": [{"model": "gpt-5.6-sol", "effort": "medium"}]}]}
```

If needed, the AI reads the surrounding conversation with `recent(session_id)` and checks how a document changed with `history("docs/cache.md")` before answering. The values above are illustrative.

## What it records
- **Turns**: what the user typed + the model's final answer, with the model name and reasoning effort. Messages sent before the answer stay in the same turn as added messages. Tool calls and results are not stored; only the path to the original session transcript is kept.
- **Question replies**: choice questions the AI asked and the option the user picked (including the options not picked).
- **Document history**: body copies and unified diffs of the project's `.md` files and of global rule files (`~/.claude/CLAUDE.md`, etc.), marked as changed by a conversation or by a person. Other produced files (pptx, pdf, png, …) keep only path, hash and size.
- Files are the source of truth; the database keeps copies and history. The search index is Korean-aware full text (lindera ko-dic); there are no embeddings.

## Requirements
- Tested on macOS only. Linux will probably work but is untested, and you run the server with systemd or similar instead of launchd. Windows is not supported (the installer uses Unix file locking `fcntl` and POSIX shell hook commands).
- Rust 1.85+ (edition 2024), Python 3 (for the capture hooks; 3.11+ when an existing `.codex/config.toml` must be inspected).
- The first build downloads the Korean dictionary (lindera ko-dic) and embeds it in the binary, so it needs internet access.

## Install
1. Build and place the binary.
   ```bash
   cargo build --release --locked
   mkdir -p ~/.local/bin ~/.ieodo/logs
   cp target/release/ieodo ~/.local/bin/ieodo
   ```
2. Keep the server running (macOS). Fill `HOME_DIR` in `contrib/com.ieodo.server.plist` and load it.
   ```bash
   sed "s|HOME_DIR|$HOME|g" contrib/com.ieodo.server.plist > ~/Library/LaunchAgents/com.ieodo.server.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ieodo.server.plist
   curl -s http://127.0.0.1:7341/health
   ```
   After code changes, `./deploy.sh` rebuilds, replaces the binary and restarts the service.
3. Connect your AI clients to the MCP server. The client name (`/mcp/<name>`, `--client <name>`) only labels the records.
   - Claude Code: `claude mcp add --transport http --scope user ieodo http://127.0.0.1:7341/mcp/claude-code`
   - Codex (`~/.codex/config.toml`):
     ```toml
     [mcp_servers.ieodo]
     command = "/Users/you/.local/bin/ieodo"
     args = ["connect", "--client", "codex"]
     ```
   - Antigravity (`mcpServers` in `~/.gemini/antigravity/mcp_config.json`): `"ieodo": {"command": "/Users/you/.local/bin/ieodo", "args": ["connect", "--client", "agy"]}`
4. Install the capture hooks in each project you want recorded. Folders that are not registered are never recorded.
   ```bash
   ieodo setup --project my-app --project-dir /absolute/path/to/my-app --harness claude --check
   ieodo setup --project my-app --project-dir /absolute/path/to/my-app --harness claude --apply
   ```
   Use `--harness codex`, `antigravity`, `both` (Codex + Claude) or `all`. Only ieodo-owned hooks are added or replaced, and changed files are backed up. Hooks start with the next session. The hook files (`.claude/settings.local.json`, `.codex/hooks.json`, `.agents/hooks.json`) contain absolute paths, so keep them out of git. Codex may ask you to trust the hooks once (`/hooks`).

## MCP tools
| Tool | What it does | Used by |
|---|---|---|
| `search` | Finds words in turns, documents and question replies; each hit carries session/turn or document path and the answering model | AI |
| `recent` | Latest turns of a project or session (newest first), with added messages, models, question replies and changed documents | AI |
| `history` | Changes of one document (time, session/turn, diff) | AI |
| `record` | Writes one session batch | capture hooks only |

## Capture hooks
`integrations/continuity/ieodo_hooks.py` (Python standard library only) runs as a hook in Claude Code, Codex and Antigravity. It is embedded in the binary, so the source folder is not needed after install.

| Hook | What it does |
|---|---|
| `UserPromptSubmit` | Opens a turn with what the user typed |
| `Stop` | Fills the final answer; files changed during the answer are credited to this turn |
| `SessionStart` | Registers the session; files changed since the last answer are recorded as edited by a person |
| `SessionEnd` | Flushes what is left |

- Turns the hooks missed (installed mid-session, hook errors) are recovered from the session transcript, along with models, effort and question replies.
- Document changes are found by comparing folder snapshots (size, mtime, hash). Paths ignored by git are skipped; nested git repositories use their own ignore rules.
- Pending items wait in a local SQLite queue and are removed only after the server accepts them.
- Antigravity hooks do not include the prompt or the answer, so both are read from its transcript (`transcript_full.jsonl`, else `transcript.jsonl`). Subagent conversations are not captured. When a message starts subagents or background tasks, the answer is held until they report back (up to 2 hours), and the final answer after those reports is stored.

## Run
```bash
ieodo serve                 # the shared server (127.0.0.1:7341)
ieodo connect --client agy  # stdio relay for stdio-only clients
ieodo stdio --client test   # open the database directly, no server
```
HTTP clients connect to `http://127.0.0.1:7341/mcp/<client-name>`. `/health` returns row counts.

Environment: `IEODO_HOME` (default `~/.ieodo`), `IEODO_ALLOW_REMOTE=1` (allow non-loopback addresses).

## Tests
```bash
cargo test --locked
python3 -m unittest discover -s integrations/continuity -p 'test_*.py' -v
```

## Limits
- Redaction catches known formats (API keys, tokens, `PASSWORD=`-style lines, …). It cannot promise to catch every secret.
- `tests/setup.rs` starts a real server and expects a hook to finish sending within 1.2 s. On a very busy machine the debug build can time out (`cargo test --release` passes).
- Antigravity hooks were checked against the official docs (October 2026) and real conversations. A server started by the same message never reports back, so it holds that answer until your next message; the answer is not lost.

## Changes
See [CHANGELOG.md](CHANGELOG.md).

## License
MIT. See [LICENSE](LICENSE).
