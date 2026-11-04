#!/usr/bin/env python3
"""Opt-in, stdlib capture of turn pairs and document changes for ieodo.

Config: {"projects":[{"root":"/absolute/repo","project":"slug"}],
"state_dir":"/private/state", "server_url":"http://127.0.0.1:7341",
"flush_limit":200, "global_documents":["~/.claude/CLAUDE.md", ...]}.

Each hook sends one session batch to ieodo's `record` tool
(wiki/schema.md, "수집기 ②"):
- The user's words come from UserPromptSubmit's `prompt`, the answer from
  Stop's `last_assistant_message`. Pairs the hooks missed are rebuilt from the
  session transcript, which also gives the model and effort of each turn and
  the answers to button questions.
- Documents are found by comparing the project folder with the last snapshot.
  Changes found when an answer ends belong to that turn; changes found when the
  user starts were made by a person and carry no turn.
Only an allowlisted cwd is accepted. Nothing is injected into a session.
Redaction is best effort, never a secret guarantee.
"""
import argparse
import calendar
import contextlib
import copy
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error


OWNER = "ieodo-continuity-v1"
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
# Antigravity gives no prompt or answer to its hooks; both are read from the
# transcript. PreInvocation runs before every model call of an answer.
ANTIGRAVITY_EVENTS = ("SessionStart", "PreInvocation", "Stop")
ANTIGRAVITY_HOOK = "ieodo-continuity"
# Hooks installed before the rename to ieodo (2026-11-04); setup replaces them.
OWNED_COMMANDS = (("ieodo_hooks.py", OWNER), ("groundlink_hooks.py", "groundlink-continuity-v1"))
LEGACY_ANTIGRAVITY_HOOKS = ("groundlink-continuity",)
CLIENTS = {"claude": "claude-code", "codex": "codex", "antigravity": "antigravity"}
HOOK_FILES = {"claude": ".claude/settings.local.json", "codex": ".codex/hooks.json", "antigravity": ".agents/hooks.json"}

# Blocks a harness appends to the user's words (Claude Code, Codex). They are
# cut away; a line-start block with any other name is kept and reported, so a
# harness changing its format shows up instead of eating the user's text.
INJECTED_TAGS = (
    "system-reminder", "task-notification", "local-command-stdout", "local-command-caveat",
    "command-message", "command-args", "send_user_message_question_reply",
    "environment_context", "external_codex_apps_open_page", "in-app-browser-context", "heartbeat",
    "INSTRUCTIONS", "turn_aborted", "agent-message", "bash-input", "bash-stdout", "bash-stderr",
    "ADDITIONAL_METADATA", "USER_SETTINGS_CHANGE",
)
INTERRUPTED = ("[Request interrupted by user]", "[Request interrupted by user for tool use]")
MAX_BATCH_BYTES = 40 * 1024 * 1024  # the server accepts 50 MiB per request
# The Codex app opens hidden sessions in the project to suggest the user's
# next message; nothing in them is the user's.
APP_SUGGESTION = "This is an app-generated request for a suggested next user message"
# A session counts as working from a prompt to its answer, for at most this long
# (an interrupted turn never gets its Stop).
WORKING_SECONDS = 2 * 60 * 60
# Claude can run Stop a moment before it writes the answer line, and the model
# is read only from that line (Antigravity: the answer itself).
ANSWER_WAIT_SECONDS = 1.0

DOCUMENT_KINDS = ("harness", "skill", "rule", "report", "record", "readme", "etc")
ARTIFACT_EXTENSIONS = frozenset("""
    pptx ppt pptm odp key docx doc docm odt rtf pages hwp hwpx xlsx xls xlsm ods numbers pdf
    png jpg jpeg gif webp svg bmp tiff tif heic mp4 mov webm mp3 wav m4a
    json csv tsv xml yaml yml html htm txt zip tar gz 7z""".split())
SKIP_DIRS = frozenset((".git", "target", "node_modules", "Saved", "Intermediate", "build", "dist",
                       ".venv", "venv", "__pycache__", ".obsidian"))
GLOBAL_DOCUMENTS = ("~/.claude/CLAUDE.md", "~/.codex/AGENTS.md", "~/.gemini/GEMINI.md", "~/.claude/skills/*/SKILL.md")
MAX_BODY_BYTES = 8 * 1024 * 1024
PATH_KEYS = ("path", "from", "to", "root_path", "transcript_path")
LISTS = ("turns", "messages", "models", "documents", "replies")


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def seconds_between(start, end):
    return (calendar.timegm(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S"))
            - calendar.timegm(time.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")))


# A name that holds a secret: the secret word ends it (DB_PASSWORD, api_key,
# GITHUB_TOKEN), so max_tokens, tokenizer and token_count are left alone. The
# server (src/text.rs) uses the same rule; keep the two in step.
SECRET_NAME = r"(?:[A-Za-z0-9]+_)*(?:TOKEN|SECRET|PASSWORD|CREDENTIALS?|API_KEY|PRIVATE_KEY|AUTH_KEY|ACCESS_KEY)(?:_KEY)?"


def redact(text):
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", text)
    text = re.sub(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
                  r"|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}"
                  r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b", "[REDACTED API KEY]", text)
    text = re.sub(r"(?i)(\b(?:" + SECRET_NAME + r"|authorization)\b[\"']?\s*[:=]\s*[\"']?)(?:Bearer\s+)?[^\s\"',;}]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", "Bearer [REDACTED]", text)
    text = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
    return text


def safe_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    if isinstance(value, dict):
        # Paths identify documents and sessions, so they stay as given.
        return {key: item if key in PATH_KEYS else "[REDACTED]" if re.fullmatch(r"(?i)" + SECRET_NAME, key)
                else safe_value(item) for key, item in value.items()}
    return value


class PermanentReject(RuntimeError):
    """The server refused a request it will refuse again."""


_CUT = re.compile(r"(?ms)^[ \t]*<(" + "|".join(map(re.escape, INJECTED_TAGS)) + r")(?:\s[^>\n]*)?>.*?</\1>[ \t]*\n?")
_LINE_BLOCK = re.compile(r"(?ms)^[ \t]*<([A-Za-z][A-Za-z0-9_-]*)(?:\s[^>\n]*)?>.*?</\1>")


_IMAGE = re.compile(r"(?ms)^[ \t]*<image\b([^>\n]*?)(?:/>|>.*?</image>)")


def _image_placeholder(match):
    found = re.search(r"\b(?:name|path|src|file)=\"([^\"]+)\"", match.group(1))
    return f"[이미지: {Path(found.group(1)).name}]" if found else "[이미지]"


def strip_injected(text, harness):
    """The user's own words, and the names of unknown line-start blocks kept."""
    text = text.replace("\r\n", "\n")
    text = _IMAGE.sub(_image_placeholder, text)
    if harness == "codex":
        text = re.sub(r"(?m)^# AGENTS\.md instructions[^\n]*\n?", "", text)
    if harness == "antigravity":
        text = re.sub(r"(?m)^[ \t]*</?USER_REQUEST>[ \t]*\n?", "", text)
    # Blocks can sit side by side on one line; cut until none is left.
    cut = None
    while cut != text:
        cut, text = text, _CUT.sub("", text)
    text = re.sub(r"<command-name>(.*?)</command-name>", r"\1", text)
    text = "\n".join(line for line in text.split("\n") if line.strip() not in INTERRUPTED)
    unknown = list(dict.fromkeys(m.group(1) for m in _LINE_BLOCK.finditer(text)))
    return text.strip(), unknown


def document_kind(path, root, head):
    """The document kind of a path, "artifact", or None when it is not tracked."""
    path = Path(path)
    if path.suffix.lower() != ".md":
        return "artifact" if path.suffix[1:].lower() in ARTIFACT_EXTENSIONS else None
    if head.startswith("---"):
        for line in head.split("\n")[1:]:
            if line.strip() == "---":
                break
            match = re.fullmatch(r"\s*kind:\s*['\"]?([A-Za-z]+)['\"]?\s*", line)
            if match and match.group(1) in DOCUMENT_KINDS:
                return match.group(1)
    name = path.name
    for suffix, kind in (("-skill.md", "skill"), ("-rule.md", "rule"), ("-report.md", "report")):
        if name.endswith(suffix):
            return kind
    if name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        return "harness"
    if name in ("STATUS.md", "HISTORY.md"):
        return "record"
    if name == "README.md":
        return "readme"
    if name == "SKILL.md" and path.parent.parent.name == "skills":
        return "skill"
    if root is not None:
        try:
            if Path(path).relative_to(root).parts[0] == "wiki":
                return "record"
        except ValueError:
            pass
    return "etc"


def file_sha256(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def git_top(path):
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, timeout=5, check=True).stdout
        return Path(out.decode("utf-8", "surrogateescape").strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return None


def project_files(root):
    """Files under a project root: what git does not ignore, or a walk.

    Git lists only when the root is a repository's top: a folder inside a
    larger repository (a home directory under git) is walked instead. A
    repository nested inside is read with its own git."""
    root = Path(root)
    if git_top(root) == root.resolve():
        try:
            listed = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                                    capture_output=True, timeout=5, check=True).stdout
            found = []
            for name in listed.decode("utf-8", "surrogateescape").split("\0"):
                # git shows a nested repository as one "name/" entry.
                if name.endswith("/") and (root / name / ".git").exists():
                    found.extend(project_files(root / name.rstrip("/")))
                elif name:
                    found.append(root / name)
            return found
        except (OSError, subprocess.SubprocessError):
            pass
    found = []
    for directory, dirs, files in os.walk(root):
        nested = [d for d in dirs if d not in SKIP_DIRS and (Path(directory) / d / ".git").exists()]
        for name in nested:
            found.extend(project_files(Path(directory) / name))
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in nested]
        found.extend(Path(directory) / name for name in files)
    return found


def owned(handler):
    """True for a command hook this adapter installed, under its current or old name."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    try:
        words = shlex.split(handler.get("command", ""))
    except ValueError:
        return False
    names = {Path(word).name for word in words}
    return any(script in names and any(words[i:i + 2] == ["--owner", owner] for i in range(len(words) - 1))
               for script, owner in OWNED_COMMANDS)


def merge_antigravity_hooks(existing, command):
    """Antigravity's hooks.json maps hook names to events; ours is one name.
    The event is passed on the command line: the hook input does not name it."""
    merged = copy.deepcopy(existing)
    # A user who switched the hook off keeps it off.
    olds = [existing.get(name) for name in (ANTIGRAVITY_HOOK, *LEGACY_ANTIGRAVITY_HOOKS)]
    off = any(isinstance(h, dict) and h.get("enabled") is False
              for old in olds if isinstance(old, dict)
              for g in old.values() if isinstance(g, list) for h in g)
    for name in LEGACY_ANTIGRAVITY_HOOKS:
        merged.pop(name, None)
    merged[ANTIGRAVITY_HOOK] = {event: [{"type": "command", "command": command + " --event " + event,
                                         "timeout": 3 if event == "PreInvocation" else 10,
                                         **({"enabled": False} if off else {})}]
                                for event in ANTIGRAVITY_EVENTS}
    return merged


def merge_hooks(existing, command, harness):
    if harness == "antigravity":
        return merge_antigravity_hooks(existing, command)
    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError("existing hook event must be a list")
        kept = []
        for group in groups:
            group = copy.deepcopy(group)
            handlers = group.get("hooks", [])
            # Remove only exact owner-marked commands, keeping unrelated handlers in a group.
            group["hooks"] = [h for h in handlers if not owned(h)]
            if group["hooks"]:
                kept.append(group)
        hooks[event] = kept
    for event in EVENTS:
        handler = {"type": "command", "command": command, "timeout": 3 if event == "SessionEnd" else 8}
        hooks.setdefault(event, []).append({"hooks": [handler]})
    return merged


class Adapter:
    def __init__(self, config_path, harness):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = json.loads(self.config_path.read_text())
        self.harness = harness
        self.answer_wait = ANSWER_WAIT_SECONDS
        self.client = CLIENTS[harness]
        base = self.config.get("server_url", "http://127.0.0.1:7341").rstrip("/")
        url = urllib.parse.urlsplit(base)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("server_url must be a local HTTP origin")
        self.url = base + "/mcp/" + self.client
        state = Path(self.config.get("state_dir", str(self.config_path.parent / "state"))).expanduser().resolve()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = state / "capture-v2.sqlite3"
        self.db = sqlite3.connect(self.db_path, timeout=5)
        os.chmod(self.db_path, 0o600)
        self.warnings = []
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS session(session TEXT PRIMARY KEY, project TEXT, root TEXT, started_at TEXT,
            transcript TEXT, cursor INTEGER DEFAULT 0, walk TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS turn(session TEXT, turn_no INTEGER, user_text TEXT, user_at TEXT,
            prompt_id TEXT, source TEXT, answered INTEGER DEFAULT 0, PRIMARY KEY(session, turn_no));
        CREATE TABLE IF NOT EXISTS message(session TEXT, turn_no INTEGER, seq INTEGER, text TEXT, sent_at TEXT,
            prompt_id TEXT, walked INTEGER DEFAULT 0, PRIMARY KEY(session, turn_no, seq));
        CREATE TABLE IF NOT EXISTS model_seen(session TEXT, turn_no INTEGER, model TEXT, effort TEXT,
            PRIMARY KEY(session, turn_no, model, effort));
        CREATE TABLE IF NOT EXISTS question(session TEXT, call_id TEXT, turn_no INTEGER, questions TEXT,
            PRIMARY KEY(session, call_id));
        CREATE TABLE IF NOT EXISTS document(project TEXT, path TEXT, size INTEGER, mtime_ns INTEGER,
            sha256 TEXT, sent INTEGER, PRIMARY KEY(project, path));
        CREATE TABLE IF NOT EXISTS scanned(project TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,
            session TEXT, list TEXT, item TEXT);
        CREATE TABLE IF NOT EXISTS quarantine(key TEXT PRIMARY KEY, session TEXT, list TEXT, item TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS unknown_tag(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS ignored_session(session TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS skipped_prompt(session TEXT, prompt_id TEXT, PRIMARY KEY(session, prompt_id));
        CREATE TABLE IF NOT EXISTS activity(session TEXT PRIMARY KEY, project TEXT, working INTEGER, since REAL);
        CREATE TABLE IF NOT EXISTS health(key TEXT PRIMARY KEY, value TEXT);
        """)

    def failure(self, error):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO health VALUES('last_error',?)", (redact(str(error)),))
            self.db.execute("INSERT OR REPLACE INTO health VALUES('last_error_at',?)", (now(),))

    def status(self):
        health = dict(self.db.execute("SELECT key,value FROM health"))
        return {"pending": self.pending(), "state_db": str(self.db_path), "harness": self.harness,
                "quarantined": self.db.execute("SELECT count(*) FROM quarantine").fetchone()[0],
                "sessions": self.db.execute("SELECT count(*) FROM session").fetchone()[0],
                "unknown_tags": [row[0] for row in self.db.execute("SELECT name FROM unknown_tag ORDER BY name")],
                "last_error": health.get("last_error"), "last_error_at": health.get("last_error_at")}

    def project(self, cwd):
        cwd = Path(cwd).expanduser().resolve()
        choices = []
        for entry in self.config.get("projects", []):
            root = Path(entry["root"]).expanduser()
            if not root.is_absolute():
                raise ValueError("project root must be absolute")
            root = root.resolve()
            if cwd == root or root in cwd.parents:
                if not entry.get("project"):
                    raise ValueError("project slug required")
                choices.append((len(root.parts), entry))
        if not choices:
            raise ValueError("cwd is outside configured project roots; capture refused")
        return max(choices, key=lambda item: item[0])[1]

    def identity(self, session_id):
        prefix = self.harness + ":"
        return session_id if session_id.startswith(prefix) else prefix + session_id

    def call(self, name, arguments, timeout=2.0):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        request = urllib.request.Request(self.url, packed(payload).encode(), {"Content-Type": "application/json", "Accept": "application/json"})
        # Deliberately ignore ambient HTTP proxies for loopback.
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=min(3.0, timeout)) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 413, 422):
                raise PermanentReject("ieodo HTTP rejection " + str(exc.code)) from exc
            raise
        if reply.get("error") or not isinstance(reply.get("result"), dict) or reply["result"].get("isError"):
            detail = " ".join(c.get("text", "") for c in reply.get("result", {}).get("content", []) if isinstance(c, dict))
            if re.search(r"exceeds|must be|identity conflict|limit|already belongs|invalid", detail, re.I):
                raise PermanentReject("ieodo rejected " + name + ": " + redact(detail[:500]))
            raise RuntimeError("ieodo rejected " + name)
        return reply["result"]

    # Queue

    def enqueue(self, key, session, list_name, item):
        # Redact *before* any payload reaches disk.
        self.db.execute("""INSERT INTO outbox(key,session,list,item) VALUES(?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET item=excluded.item""", (key, session, list_name, packed(safe_value(item))))

    def pending(self):
        return self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]

    def envelope(self, sid):
        project, root, started_at, transcript = self.db.execute(
            "SELECT project,root,started_at,transcript FROM session WHERE session=?", (sid,)).fetchone()
        session = {"session_id": sid, "harness": self.client, "started_at": started_at}
        if transcript:
            session["transcript_path"] = transcript
        return {"project": {"name": project, "root_path": root}, "session": session}

    def flush(self, budget=2.2):
        start = time.monotonic()
        sent = 0
        error = None
        limit = max(1, min(500, int(self.config.get("flush_limit", 200))))
        sessions = [row[0] for row in self.db.execute("SELECT session FROM outbox GROUP BY session ORDER BY min(seq)")]
        for sid in sessions:
            remaining = budget - (time.monotonic() - start)
            if remaining <= 0.05:
                break
            rows, size = [], 0
            for row in self.db.execute("SELECT key,list,item FROM outbox WHERE session=? ORDER BY seq LIMIT ?", (sid, limit)):
                if rows and size + len(row[2]) > MAX_BATCH_BYTES:
                    break
                rows.append(row)
                size += len(row[2])
            items = {name: [] for name in LISTS}
            for row in sorted(rows, key=lambda r: json.loads(r[2])["turn_no"] if r[1] == "turns" else 0):
                items[row[1]].append(row)
            args = self.envelope(sid)
            where = {}
            for name in LISTS:
                if items[name]:
                    args[name] = [json.loads(row[2]) for row in items[name]]
                    where.update({f"{name}[{index}]": row for index, row in enumerate(items[name])})
            try:
                result = self.call("record", args, timeout=remaining)
                text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
                failed = {entry.get("item"): str(entry.get("error", "")) for entry in json.loads(text).get("errors", [])}
            except PermanentReject as exc:
                failed = {name: str(exc) for name in where}
                error = "PermanentReject: " + str(exc)
                self.failure(error)
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc)
                self.failure(error)
                break
            with self.db:
                for name, (key, list_name, item) in where.items():
                    if name in failed:
                        self.db.execute("INSERT OR REPLACE INTO quarantine VALUES(?,?,?,?,?)",
                                        (key, sid, list_name, item, redact(failed[name])))
                    else:
                        sent += 1
                    # A newer version queued while this one was in flight stays.
                    self.db.execute("DELETE FROM outbox WHERE key=? AND item=?", (key, item))
        return {"sent": sent, "pending": self.pending(), "error": error}

    # Turn pairs

    def queue_turn(self, sid, turn_no, answer=None, answered_at=None):
        user_text, user_at = self.db.execute("SELECT user_text,user_at FROM turn WHERE session=? AND turn_no=?",
                                             (sid, turn_no)).fetchone()
        item = {"turn_no": turn_no, "user_text": user_text, "user_at": user_at}
        if answer:
            item.update(assistant_text=redact(answer), assistant_at=answered_at or now())
            self.db.execute("UPDATE turn SET answered=1 WHERE session=? AND turn_no=?", (sid, turn_no))
        self.enqueue(f"turn:{sid}:{turn_no}", sid, "turns", item)

    def new_turn(self, sid, turn_no, text, at, source, prompt_id=None):
        self.db.execute("INSERT INTO turn(session,turn_no,user_text,user_at,prompt_id,source) VALUES(?,?,?,?,?,?)",
                        (sid, turn_no, redact(text), at, prompt_id, source))
        self.queue_turn(sid, turn_no)

    def open_turn(self, sid, text, prompt_id, walked):
        text = redact(text)
        last = self.db.execute("SELECT turn_no,user_text,source,prompt_id,answered FROM turn WHERE session=? "
                               "ORDER BY turn_no DESC LIMIT 1", (sid,)).fetchone()
        # The transcript may already hold the prompt this hook carries.
        if last and last[2] == "transcript" and last[3] is None and not last[4] and last[1] == text:
            self.db.execute("UPDATE turn SET source='hook', prompt_id=? WHERE session=? AND turn_no=?",
                            (prompt_id or "", sid, last[0]))
            return
        turn_no = last[0] if last else 0
        # The transcript may already hold this added message too.
        added = self.db.execute("SELECT seq FROM message WHERE session=? AND turn_no=? AND text=? AND prompt_id IS NULL "
                                "ORDER BY seq LIMIT 1", (sid, turn_no, text)).fetchone()
        if added:
            self.db.execute("UPDATE message SET prompt_id=? WHERE session=? AND turn_no=? AND seq=?",
                            (prompt_id or "", sid, turn_no, added[0]))
            return
        state = self.walk_state if self.walk_state.get("turn") == turn_no else {}
        if turn_no == self.unanswered_turn(sid, now()) and not state.get("parts") and not state.get("interrupted"):
            self.add_message(sid, turn_no, text, now(), prompt_id or "")
            return
        self.new_turn(sid, max(turn_no, walked) + 1, text, now(), "hook", prompt_id or "")

    def unanswered_turn(self, sid, at):
        """The latest turn when it still waits for its answer and is not stale."""
        row = self.db.execute("SELECT turn_no,user_at,answered FROM turn WHERE session=? ORDER BY turn_no DESC LIMIT 1",
                              (sid,)).fetchone()
        if not row or row[2] or seconds_between(row[1], at) > WORKING_SECONDS:
            return None
        return row[0]

    def add_message(self, sid, turn_no, text, at, prompt_id=None, walked=0):
        """A message sent before the answer: the turn's text cannot change, so it goes beside it."""
        text = redact(text)
        seq = self.db.execute("SELECT coalesce(max(seq),0)+1 FROM message WHERE session=? AND turn_no=?",
                              (sid, turn_no)).fetchone()[0]
        self.db.execute("INSERT INTO message VALUES(?,?,?,?,?,?,?)", (sid, turn_no, seq, text, at, prompt_id, walked))
        self.enqueue(f"message:{sid}:{turn_no}:{seq}", sid, "messages",
                     {"turn_no": turn_no, "seq": seq, "text": text, "sent_at": at})

    def answer(self, sid, payload):
        message = payload.get("last_assistant_message")
        if not isinstance(message, str) or not message.strip():
            return
        # Claude names the prompt being answered; a prompt that made no turn
        # (a background report, a reply) has no pair to answer. Codex does not
        # name it, so the latest open turn is the one.
        prompt_id = payload.get("prompt_id")
        if prompt_id and self.db.execute("SELECT 1 FROM skipped_prompt WHERE session=? AND prompt_id=?",
                                         (sid, prompt_id)).fetchone():
            return
        row = None
        if prompt_id:
            row = self.db.execute("SELECT turn_no,answered FROM turn WHERE session=? AND prompt_id=?",
                                  (sid, prompt_id)).fetchone()
        if row is None and prompt_id:
            added = self.db.execute("SELECT turn_no FROM message WHERE session=? AND prompt_id=?",
                                    (sid, prompt_id)).fetchone()
            if added:
                row = self.db.execute("SELECT turn_no,answered FROM turn WHERE session=? AND turn_no=?",
                                      (sid, added[0])).fetchone()
        if row is None and prompt_id:
            # A prompt the hooks never saw (installed mid-turn): the latest
            # turn rebuilt from the transcript.
            row = self.db.execute("SELECT turn_no,answered FROM turn WHERE session=? AND prompt_id IS NULL "
                                  "ORDER BY turn_no DESC LIMIT 1", (sid,)).fetchone()
        elif row is None:
            row = self.db.execute("SELECT turn_no,answered FROM turn WHERE session=? AND answered=0 "
                                  "ORDER BY turn_no DESC LIMIT 1", (sid,)).fetchone()
        if row and not row[1]:
            self.queue_turn(sid, row[0], message)

    def note_unknown(self, names):
        for name in names:
            if self.db.execute("INSERT OR IGNORE INTO unknown_tag VALUES(?)", (name,)).rowcount:
                self.warnings.append("unknown block kept in user text: <" + name + ">")

    def queue_model(self, sid, turn_no, model, effort):
        if turn_no < 1 or not isinstance(model, str) or not model:
            return
        effort = effort if isinstance(effort, str) else ""
        if self.db.execute("INSERT OR IGNORE INTO model_seen VALUES(?,?,?,?)", (sid, turn_no, model, effort)).rowcount:
            item = {"turn_no": turn_no, "model": model}
            if effort:
                item["effort"] = effort
            self.enqueue(f"model:{sid}:{turn_no}:{model}:{effort}", sid, "models", item)

    def queue_reply(self, sid, turn_no, question_id, question, answer, options, answered_at):
        item = {"turn_no": turn_no, "question_id": question_id, "question": question, "answer": answer,
                "options": [str(option) for option in options]}
        if answered_at:
            item["answered_at"] = answered_at
        self.enqueue(f"reply:{sid}:{question_id}", sid, "replies", item)

    def close_walked_turn(self, sid, walk):
        turn_no = walk.get("turn", 0)
        if turn_no < 1 or walk.get("interrupted") or not walk.get("parts"):
            return
        row = self.db.execute("SELECT answered FROM turn WHERE session=? AND turn_no=?", (sid, turn_no)).fetchone()
        if row and not row[0]:
            self.queue_turn(sid, turn_no, "\n\n".join(walk["parts"]), walk.get("answer_at"))

    def walk(self, sid, transcript, closing):
        """Reads new transcript lines: missed pairs, models and replies."""
        cursor, walk = self.db.execute("SELECT cursor,walk FROM session WHERE session=?", (sid,)).fetchone()
        walk = json.loads(walk or "{}")
        lines = []
        if transcript and Path(transcript).is_file():
            with open(transcript, "rb") as stream:
                if os.fstat(stream.fileno()).st_size < cursor:
                    cursor, walk = 0, {}
                stream.seek(cursor)
                data = stream.read()
            complete = data[:data.rfind(b"\n") + 1]
            cursor += len(complete)
            lines = complete.split(b"\n")[:-1]
        for raw in lines:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                self.walk_record(sid, record, walk)
        if closing:
            # Antigravity wakes the conversation again when a subagent or a
            # background task it started finishes; the answer is the last one.
            walk["waiting"] = self.awaiting(walk)
            if not walk["waiting"]:
                self.close_walked_turn(sid, walk)
        self.db.execute("UPDATE session SET cursor=?, walk=? WHERE session=?", (cursor, packed(walk), sid))
        self.walk_state = walk
        return walk.get("turn", 0)

    @staticmethod
    def awaiting(walk):
        """Started work that has not reported back, for at most WORKING_SECONDS."""
        pending = walk.get("pending") or {}
        for key, started in list(pending.items()):
            try:
                age = seconds_between(started, now())
            except (TypeError, ValueError):
                age = 0
            if age > WORKING_SECONDS:
                del pending[key]
        return bool(pending)

    def wait_for_answer(self, sid, transcript):
        """Waits briefly until an answer line follows the walk's cursor."""
        answer = {"claude": "assistant", "antigravity": "PLANNER_RESPONSE"}.get(self.harness)
        if not answer or not transcript or not Path(transcript).is_file():
            return
        row = self.db.execute("SELECT cursor FROM session WHERE session=?", (sid,)).fetchone()
        cursor = row[0] if row else 0
        deadline = time.monotonic() + self.answer_wait
        while True:
            with open(transcript, "rb") as stream:
                stream.seek(cursor)
                data = stream.read()
            for raw in data[:data.rfind(b"\n") + 1].split(b"\n"):
                if b'"' + answer.encode() + b'"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                # Antigravity writes a response line for every tool call too.
                if isinstance(record, dict) and record.get("type") == answer and not record.get("tool_calls"):
                    return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def walk_prompt(self, sid, text, at, walk):
        clean, unknown = strip_injected(text, self.harness)
        if text.strip() in INTERRUPTED or text.lstrip().startswith("<turn_aborted>"):
            walk["interrupted"] = True
        if not clean:
            return
        self.note_unknown(unknown)
        at = at or now()
        current = walk.get("turn", 0)
        # A hook may have added this message before the walk reached it.
        added = self.db.execute("SELECT seq FROM message WHERE session=? AND turn_no=? AND text=? AND walked=0 "
                                "ORDER BY seq LIMIT 1", (sid, current, redact(clean))).fetchone()
        if added:
            self.db.execute("UPDATE message SET walked=1 WHERE session=? AND turn_no=? AND seq=?", (sid, current, added[0]))
            return
        if (current >= 1 and current == self.unanswered_turn(sid, at)
                and not walk.get("parts") and not walk.get("interrupted")):
            self.add_message(sid, current, clean, at, walked=1)
            return
        self.close_walked_turn(sid, walk)
        turn_no = current + 1
        # Only work started for this message holds its answer (a server left
        # running by an earlier one never reports back).
        walk.update(turn=turn_no, parts=[], answer_at=None, interrupted=False, waiting=False, pending={})
        if not self.db.execute("SELECT 1 FROM turn WHERE session=? AND turn_no=?", (sid, turn_no)).fetchone():
            self.new_turn(sid, turn_no, clean, at, "transcript")

    def walk_record(self, sid, record, walk):
        at = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        turn_no = walk.get("turn", 0)
        if self.harness == "antigravity":
            self.walk_antigravity(sid, record, walk)
            return
        if self.harness == "codex":
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return
            if record.get("type") == "turn_context":
                walk["context"] = {key: payload[key] for key in ("model", "effort") if isinstance(payload.get(key), str)}
                return
            if record.get("type") != "response_item":
                return
            kind = payload.get("type")
            if kind == "message":
                content = payload.get("content")
                blocks = content if isinstance(content, list) else []
                text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str))
                if payload.get("role") == "user":
                    self.codex_replies(sid, text, at)
                    self.walk_prompt(sid, text, at, walk)
                elif payload.get("role") == "assistant" and text.strip():
                    walk.setdefault("parts", []).append(redact(text))
                    walk["answer_at"] = at
                    context = walk.get("context") or {}
                    self.queue_model(sid, turn_no, context.get("model"), context.get("effort"))
            elif kind in ("function_call", "custom_tool_call"):
                walk["parts"] = []
                if payload.get("name") == "request_user_input_async" and turn_no >= 1:
                    try:
                        questions = json.loads(payload.get("arguments") or "{}").get("questions", [])
                    except (ValueError, AttributeError):
                        questions = []
                    self.db.execute("INSERT OR REPLACE INTO question VALUES(?,?,?,?)",
                                    (sid, str(payload.get("call_id", "")), turn_no, packed(safe_value(questions))))
            return
        attachment = record.get("attachment")
        if record.get("type") == "attachment" and isinstance(attachment, dict):
            # A message typed while the answer was running is written only as
            # a queued command.
            if (attachment.get("type") == "queued_command" and (attachment.get("origin") or {}).get("kind") == "human"
                    and isinstance(attachment.get("prompt"), str)):
                stamp = attachment.get("timestamp") if isinstance(attachment.get("timestamp"), str) else at
                self.walk_prompt(sid, attachment["prompt"], stamp, walk)
            return
        if record.get("isMeta") or record.get("isCompactSummary") or record.get("type") not in ("user", "assistant"):
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content if isinstance(content, list) else []
        if record["type"] == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.claude_replies(sid, str(block.get("tool_use_id", "")), record.get("toolUseResult"), at)
            text = "\n".join(b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
            if text:
                self.walk_prompt(sid, text, at, walk)
            return
        self.queue_model(sid, turn_no, message.get("model"), record.get("effort"))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                walk.setdefault("parts", []).append(redact(block["text"]))
                walk["answer_at"] = at
            elif block.get("type") == "tool_use":
                walk["parts"] = []
                if block.get("name") == "AskUserQuestion" and turn_no >= 1:
                    questions = (block.get("input") or {}).get("questions", [])
                    self.db.execute("INSERT OR REPLACE INTO question VALUES(?,?,?,?)",
                                    (sid, str(block.get("id", "")), turn_no, packed(safe_value(questions))))

    def walk_antigravity(self, sid, record, walk):
        at = record.get("created_at") if isinstance(record.get("created_at"), str) else None
        content = record.get("content") if isinstance(record.get("content"), str) else ""
        kind = record.get("type")
        # Only what the user typed is a prompt; a subagent's task is not.
        if kind == "USER_INPUT" and record.get("source") == "USER_EXPLICIT":
            # The model is known only from the notice of a model change.
            change = re.search(r"`Model Selection` from .*? to (.+?)(?: \(([^()]+)\))?\.(?:\s|$)", content)
            if change:
                walk["context"] = {"model": change.group(1), **({"effort": change.group(2)} if change.group(2) else {})}
            self.walk_prompt(sid, content, at, walk)
        elif kind == "GENERIC":
            # Only the tool's own notice: output it printed may quote old ones.
            started = (re.match(r'Created At:[^\n]*\n(?:Completed At:[^\n]*\n)?Created the following subagent:.*?"conversationId":\s*"([^"]+)"',
                                content, re.S)
                       or re.match(r"Created At:[^\n]*\nTool is running as a background task with task id: (\S+)", content))
            if started:
                walk.setdefault("pending", {})[started.group(1)] = at or now()
        elif kind == "SYSTEM_MESSAGE":
            sender = re.search(r"\bsender=(\S+)", content)
            if sender and sender.group(1) in walk.get("pending", {}):
                # A report wakes the conversation; what it says next is the answer.
                del walk["pending"][sender.group(1)]
                walk["parts"] = []
        elif kind == "PLANNER_RESPONSE":
            if record.get("tool_calls"):
                walk["parts"] = []
            elif content.strip():
                walk.setdefault("parts", []).append(redact(content))
                walk["answer_at"] = at
                context = walk.get("context") or {}
                self.queue_model(sid, walk.get("turn", 0), context.get("model"), context.get("effort"))

    def claude_replies(self, sid, call_id, result, at):
        row = self.db.execute("SELECT turn_no,questions FROM question WHERE session=? AND call_id=?", (sid, call_id)).fetchone()
        if not row or not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            return
        answers = {redact(str(key)): value for key, value in result["answers"].items()}
        for index, question in enumerate(json.loads(row[1])):
            if not isinstance(question, dict) or question.get("question") not in answers:
                continue
            options = [o.get("label", "") if isinstance(o, dict) else o for o in question.get("options", [])]
            self.queue_reply(sid, row[0], f"{call_id}#{index}", question["question"],
                             str(answers[question["question"]]), options, at)

    def codex_replies(self, sid, text, at):
        for block in re.findall(r"(?s)<send_user_message_question_reply>(.*?)</send_user_message_question_reply>", text):
            try:
                entries = json.loads(block.strip())
            except ValueError:
                continue
            for entry in entries if isinstance(entries, list) else []:
                try:
                    _name, call_id, index = json.loads(entry["questionItemId"])
                    row = self.db.execute("SELECT turn_no,questions FROM question WHERE session=? AND call_id=?",
                                          (sid, call_id)).fetchone()
                    question = json.loads(row[1])[index]
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                options = question.get("options", []) if isinstance(question, dict) else []
                self.queue_reply(sid, row[0], entry["questionItemId"], str(entry.get("question", "")),
                                 str(entry.get("answer", "")), options, at)

    # Documents

    def global_documents(self):
        found = []
        for pattern in self.config.get("global_documents", GLOBAL_DOCUMENTS):
            found.extend(Path(p) for p in sorted(glob.glob(os.path.expanduser(pattern))))
        return found

    def scan(self, project, sid, turn_no):
        """Compares the project folder with the last snapshot and queues the changes."""
        name = project["project"]
        root = Path(project["root"]).expanduser().resolve()
        first = not self.db.execute("SELECT 1 FROM scanned WHERE project=?", (name,)).fetchone()
        known = {row[0]: row[1:] for row in self.db.execute(
            "SELECT path,size,mtime_ns,sha256,sent FROM document WHERE project=?", (name,))}
        roots = {}
        for path in project_files(root):
            if document_kind(path, root, "") is not None:
                roots[str(path)] = root
        for path in self.global_documents():
            if document_kind(path, None, "") is not None:
                roots.setdefault(str(path), None)
        seen, changed = set(), []
        for path, path_root in roots.items():
            try:
                info = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            seen.add(path)
            old = known.get(path)
            if old and old[0] == info.st_size and old[1] == info.st_mtime_ns:
                continue
            if first and roots[path] is not None and document_kind(path, root, "") == "artifact":
                # A baseline needs no hash: only later changes are reported.
                self.remember(name, path, info, None, 0)
                continue
            sha = file_sha256(path)
            if old and old[2] == sha:
                self.db.execute("UPDATE document SET size=?, mtime_ns=? WHERE project=? AND path=?",
                                (info.st_size, info.st_mtime_ns, name, path))
                continue
            changed.append((path, path_root, info, sha, old))
        at = now()
        # The first look at a folder finds what was already there.
        origin = {"turn_no": turn_no} if turn_no and not first else {}
        vanished = [path for path in known if path not in seen]
        for path in vanished:
            sent = known[path][3]
            twin = next((c for c in changed if c[4] is None and c[3] == known[path][2]), None) if sent and known[path][2] else None
            if twin:
                changed.remove(twin)
                self.enqueue(f"doc:{sid}:{time.time_ns()}:{path}", sid, "documents",
                             {"op": "rename", "from": path, "to": twin[0], "at": at, **origin})
                self.remember(name, twin[0], twin[2], twin[3], 1)
            elif sent:
                self.enqueue(f"doc:{sid}:{time.time_ns()}:{path}", sid, "documents",
                             {"op": "delete", "path": path, "at": at, **origin})
            self.db.execute("DELETE FROM document WHERE project=? AND path=?", (name, path))
        for path, path_root, info, sha, old in changed:
            kind = document_kind(path, path_root, "")
            if kind == "artifact":
                if first:
                    self.remember(name, path, info, sha, 0)
                    continue
                item = {"op": "snapshot", "path": path, "kind": kind, "hash": "sha256:" + sha, "size": info.st_size}
            else:
                if info.st_size > MAX_BODY_BYTES:
                    self.warnings.append("document over 8 MiB skipped: " + redact(path))
                    continue
                try:
                    body = Path(path).read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    self.warnings.append("document is not UTF-8 text, skipped: " + redact(path))
                    continue
                item = {"op": "snapshot", "path": path, "kind": document_kind(path, path_root, body[:4096]), "body": body}
            self.enqueue(f"doc:{sid}:{time.time_ns()}:{path}", sid, "documents", {**item, "at": at, **origin})
            self.remember(name, path, info, sha, 1)
        self.db.execute("INSERT OR IGNORE INTO scanned VALUES(?)", (name,))

    def remember(self, project, path, info, sha, sent):
        self.db.execute("INSERT OR REPLACE INTO document VALUES(?,?,?,?,?,?)",
                        (project, path, info.st_size, info.st_mtime_ns, sha, sent))

    def capture(self, transcript, session_id, cwd):
        """Rebuilds a session from its transcript, for sessions no hook will reach again."""
        project = self.project(cwd)
        sid = self.identity(session_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT OR IGNORE INTO session(session,project,root,started_at) VALUES(?,?,?,?)",
                            (sid, project["project"], str(Path(project["root"]).expanduser().resolve()), now()))
            self.db.execute("UPDATE session SET transcript=? WHERE session=?", (str(transcript), sid))
            turns = self.walk(sid, str(transcript), True)
        return {"session_id": sid, "turns": turns, **self.flush(budget=30)}

    # Hook

    def antigravity_payload(self, payload):
        """Antigravity's hook input under the names the rest of the adapter reads."""
        paths = [urllib.parse.unquote(urllib.parse.urlsplit(p).path) if p.startswith("file://") else p
                 for p in payload.get("workspacePaths") or [] if isinstance(p, str) and p]
        cwd = next((p for p in paths if self.registered(p)), paths[0] if paths else None)
        transcript = payload.get("transcriptPath") if isinstance(payload.get("transcriptPath"), str) else None
        subagent = False
        if transcript:
            logs = Path(transcript)
            # The app also keeps an untruncated copy beside it.
            if logs.with_name("transcript_full.jsonl").is_file():
                transcript = str(logs.with_name("transcript_full.jsonl"))
            subagent = self.antigravity_subagent(logs, payload.get("conversationId"))
        return {"hook_event_name": payload.get("hook_event_name", ""), "session_id": payload.get("conversationId"),
                "cwd": cwd, "transcript_path": transcript, "model": payload.get("modelName"), "subagent": subagent}

    def registered(self, path):
        try:
            self.project(path)
            return True
        except ValueError:
            return False

    @staticmethod
    def antigravity_subagent(transcript, conversation_id):
        """A conversation another conversation started, by the app's own index."""
        # <app>/brain/<conversation>/.system_generated/logs/transcript.jsonl
        index = transcript.parents[4] / "conversation_summaries.db" if len(transcript.parents) > 4 else None
        if not index or not index.is_file() or not isinstance(conversation_id, str):
            return False
        try:
            with contextlib.closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                row = db.execute("SELECT nesting_depth, parent_conversation_id FROM conversation_summaries "
                                 "WHERE conversation_id=?", (conversation_id,)).fetchone()
        except sqlite3.Error:
            return False
        return bool(row and (row[0] or row[1]))

    def hook(self, payload):
        if self.harness == "antigravity":
            payload = self.antigravity_payload(payload)
        if not isinstance(payload.get("cwd"), str) or not payload["cwd"]:
            raise ValueError("hook cwd missing; capture refused")
        project = self.project(payload["cwd"])
        event = payload.get("hook_event_name", "")
        native = payload.get("session_id")
        if not native:
            raise ValueError("hook session_id missing")
        sid = self.identity(native)
        transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
        self.warnings = []
        prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
        with self.db:
            if payload.get("subagent") or prompt is not None and prompt.lstrip().startswith(APP_SUGGESTION):
                self.db.execute("INSERT OR IGNORE INTO ignored_session VALUES(?)", (sid,))
            if self.db.execute("SELECT 1 FROM ignored_session WHERE session=?", (sid,)).fetchone():
                return {}
            # PreInvocation comes before every model call; only the first one
            # of an answer marks where the user's message began.
            if event == "PreInvocation" and self.db.execute(
                    "SELECT 1 FROM activity WHERE session=? AND working=1", (sid,)).fetchone():
                return {}
        if event == "Stop":
            # Outside the transaction: other sessions' hooks keep going.
            self.wait_for_answer(sid, transcript)
        with self.db:
            # Turns are committed before the folder scan, so a slow scan never
            # costs the conversation.
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT OR IGNORE INTO session(session,project,root,started_at) VALUES(?,?,?,?)",
                            (sid, project["project"], str(Path(project["root"]).expanduser().resolve()), now()))
            if transcript:
                self.db.execute("UPDATE session SET transcript=? WHERE session=?", (transcript, sid))
            # Antigravity's answer is read from the transcript, so Stop closes it.
            walked = self.walk(sid, transcript, event == "SessionEnd" or (event == "Stop" and self.harness == "antigravity"))
            # Woken by its own finished work, not by the user: still the same turn.
            continuing = event == "PreInvocation" and self.walk_state.get("waiting")
            if event == "PreInvocation":
                self.db.execute("INSERT OR REPLACE INTO activity VALUES(?,?,1,?)", (sid, project["project"], time.time()))
            if event == "UserPromptSubmit" and prompt is not None:
                text, unknown = strip_injected(prompt, self.harness)
                if text:
                    self.note_unknown(unknown)
                    self.open_turn(sid, text, payload.get("prompt_id"), walked)
                    self.db.execute("INSERT OR REPLACE INTO activity VALUES(?,?,1,?)", (sid, project["project"], time.time()))
                elif payload.get("prompt_id"):
                    self.db.execute("INSERT OR IGNORE INTO skipped_prompt VALUES(?,?)", (sid, payload["prompt_id"]))
            if event == "Stop":
                self.answer(sid, payload)
                if isinstance(payload.get("model"), str) and payload["model"]:
                    # The hook names the model; a model change notice in the
                    # transcript also gives the effort.
                    last = self.db.execute("SELECT max(turn_no) FROM turn WHERE session=?", (sid,)).fetchone()[0]
                    if last and not self.db.execute("SELECT 1 FROM model_seen WHERE session=? AND turn_no=?",
                                                     (sid, last)).fetchone():
                        self.queue_model(sid, last, payload["model"], "")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # What changed while the answer ran belongs to the turn just
            # answered; what changed before the user spoke was a person.
            turn_no = None
            if event in ("Stop", "SessionEnd"):
                self.db.execute("UPDATE activity SET working=0 WHERE session=?", (sid,))
                # A folder is shared: credit the turn only when no other
                # session of the project is working at the same time.
                busy = self.db.execute("SELECT count(*) FROM activity WHERE project=? AND session!=? AND working=1 "
                                       "AND since>?", (project["project"], sid, time.time() - WORKING_SECONDS)).fetchone()[0]
                if not busy:
                    turn_no = self.db.execute("SELECT max(turn_no) FROM turn WHERE session=?", (sid,)).fetchone()[0]
            elif continuing:
                turn_no = walked
            self.scan(project, sid, turn_no)
        flushed = self.flush(budget=1.2 if event == "SessionStart" else 2.2)
        warnings = list(self.warnings)
        if flushed["error"] or flushed["pending"]:
            warnings.append(f"{flushed['pending']} items await delivery: {flushed['error'] or 'flush limit reached'}")
        output = {}
        if warnings:
            message = "ieodo capture: " + "; ".join(dict.fromkeys(warnings))
            print(message, file=sys.stderr)
            if event == "SessionStart" and self.harness != "antigravity":
                output["systemMessage"] = message
        return output


def hook_output(harness, event, output):
    """What the host reads back. Antigravity's Stop requires a decision, and
    only "continue" would keep the agent running."""
    if harness == "antigravity":
        return {"decision": "stop"} if event == "Stop" else {}
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=tuple(CLIENTS), required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--owner", default=OWNER, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    hook = sub.add_parser("hook")
    hook.add_argument("--event", help="the event, for hosts whose hook input does not name it")
    sub.add_parser("status")
    capture = sub.add_parser("capture", help="rebuild one finished session from its transcript")
    capture.add_argument("--transcript", required=True)
    capture.add_argument("--session-id", required=True)
    capture.add_argument("--project-dir", required=True)
    install = sub.add_parser("install", help="preview project hook config; --apply writes the preview")
    install.add_argument("--project-dir", required=True)
    install.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        adapter = Adapter(args.config, args.harness)
        if args.command == "hook":
            try:
                payload = json.load(sys.stdin)
                if args.event:
                    payload["hook_event_name"] = args.event
                output = adapter.hook(payload)
            except Exception as exc:
                message = "ieodo capture failed: " + type(exc).__name__ + ": " + str(exc)
                adapter.failure(message)
                print(message, file=sys.stderr)
                output = {"systemMessage": message}
            print(packed(hook_output(args.harness, args.event, output)))
            return 0  # Hooks must never stop native work; warnings are explicit.
        if args.command == "status":
            print(packed(adapter.status()))
            return 0
        if args.command == "capture":
            result = adapter.capture(args.transcript, args.session_id, args.project_dir)
            print(packed(result))
            return 1 if result["error"] or result["pending"] else 0
        adapter.project(args.project_dir)
        target = Path(args.project_dir).resolve() / HOOK_FILES[args.harness]
        existing = json.loads(target.read_text()) if target.exists() else {}
        command = " ".join(shlex.quote(str(s)) for s in [sys.executable, Path(__file__).resolve(), "--harness", args.harness,
                           "--config", adapter.config_path, "--owner", OWNER, "hook"])
        result = merge_hooks(existing, command, args.harness)
        if args.apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = target.with_name(target.name + ".ieodo-backup-" + str(time.time_ns()))
                backup.write_bytes(target.read_bytes())
                backup.chmod(0o600)
            temporary = target.with_name(target.name + ".ieodo-tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(target)
        print(packed({"path": str(target), "applied": args.apply, "config": result}))
        return 0
    except Exception as exc:
        message = "ieodo capture failed: " + type(exc).__name__ + ": " + str(exc)
        print(message, file=sys.stderr)
        if args.command == "hook":
            print(packed(hook_output(args.harness, args.event, {"systemMessage": message})))
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
