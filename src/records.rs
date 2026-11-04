//! Local records for projects, conversations, and document snapshots.
//! Writes redact secrets and keep append-only history and Korean-aware
//! full-text indexes in the same transaction.

use crate::text;
use libsql::{Builder, Connection, Database, Row, params};
use sha2::{Digest, Sha256};
use similar::TextDiff;
use std::borrow::Cow;
use std::path::{Path, PathBuf};
use tokio::sync::Mutex;

/// Storage errors with operation context.
pub type Result<T> = std::result::Result<T, String>;

/// A local database with serialized access to its connection.
pub struct Records {
    _db: Database,
    conn: Mutex<Connection>,
    path: PathBuf,
}

/// How many rows each record table holds.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct Counts {
    pub projects: i64,
    pub sessions: i64,
    pub turns: i64,
    pub documents: i64,
    pub document_changes: i64,
    pub replies: i64,
}

/// Whether a record was added and how many secrets were hidden.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordOutcome {
    pub inserted: bool,
    pub redacted: usize,
}

/// The document identity and the change recorded, if any.
#[derive(Debug, Clone, PartialEq)]
pub struct DocumentOutcome {
    pub doc_id: i64,
    pub change: Option<String>,
}

/// A harness session belonging to a known project.
#[derive(Debug, Clone, PartialEq)]
pub struct Session {
    pub session_id: String,
    pub project: String,
    pub harness: String,
    pub started_at: String,
    pub transcript_path: Option<String>,
}

/// A user message and its optional final assistant answer.
#[derive(Debug, Clone, PartialEq)]
pub struct Turn {
    pub session_id: String,
    pub turn_no: i64,
    pub user_text: String,
    pub assistant_text: Option<String>,
    pub user_at: String,
    pub assistant_at: Option<String>,
}

/// A message the user added before the turn's answer.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnMessage {
    pub session_id: String,
    pub turn_no: i64,
    pub seq: i64,
    pub text: String,
    pub sent_at: String,
}

/// A model contributing to a turn.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnModel {
    pub session_id: String,
    pub turn_no: i64,
    pub model: String,
    pub effort: Option<String>,
}

/// Text to redact, or an artifact's externally computed hash and size.
#[derive(Debug, Clone, PartialEq)]
pub enum Content {
    Text(String),
    Binary { hash: String, size: i64 },
}

/// A document snapshot and its optional origin and project association.
#[derive(Debug, Clone)]
pub struct DocumentInput {
    pub path: String,
    pub kind: String,
    pub content: Content,
    pub at: String,
    pub session_id: Option<String>,
    pub turn_no: Option<i64>,
    pub project: Option<String>,
}

/// The last collected snapshot, retained even after deletion.
#[derive(Debug, Clone, PartialEq)]
pub struct Document {
    pub doc_id: i64,
    pub name: String,
    pub kind: String,
    pub path: String,
    pub created_at: String,
    pub updated_at: String,
    pub body: Option<String>,
    pub body_hash: Option<String>,
    pub size: Option<i64>,
}

/// One immutable document change.
#[derive(Debug, Clone, PartialEq)]
pub struct DocumentChange {
    pub change_id: i64,
    pub doc_id: i64,
    pub changed_at: String,
    pub change_kind: String,
    pub session_id: Option<String>,
    pub turn_no: Option<i64>,
    pub diff: Option<String>,
    pub body_hash_after: Option<String>,
}

/// A user's answer to one harness question, including all offered options.
#[derive(Debug, Clone, PartialEq)]
pub struct QuestionReply {
    pub session_id: String,
    pub turn_no: i64,
    pub question_id: String,
    pub question: String,
    pub answer: String,
    pub options: Vec<String>,
    pub answered_at: Option<String>,
}

/// Which record kinds to search.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Scope {
    All,
    Turns,
    Documents,
    Replies,
}

/// A lexical search match; higher scores rank first.
#[derive(Debug, Clone)]
pub struct Hit {
    pub kind: String,
    pub session_id: Option<String>,
    pub turn_no: Option<i64>,
    pub doc_id: Option<i64>,
    pub path: Option<String>,
    pub excerpt: String,
    pub score: f64,
    /// The models behind the turn a turn or reply hit belongs to.
    pub models: Vec<TurnModel>,
}

/// A turn with its models, replies, and affected document paths.
#[derive(Debug, Clone)]
pub struct RecentTurn {
    pub turn: Turn,
    pub messages: Vec<TurnMessage>,
    pub models: Vec<TurnModel>,
    pub replies: Vec<QuestionReply>,
    pub changed_documents: Vec<String>,
}

fn sql_err(context: &str) -> impl Fn(libsql::Error) -> String + '_ {
    move |e| format!("{context}: {e}")
}

fn text_of(row: &Row, idx: i32) -> Result<String> {
    row.get::<String>(idx)
        .map_err(|e| format!("column {idx}: {e}"))
}

fn optional_text(row: &Row, idx: i32) -> Result<Option<String>> {
    row.get::<Option<String>>(idx)
        .map_err(|e| format!("column {idx}: {e}"))
}

fn integer_of(row: &Row, idx: i32) -> Result<i64> {
    row.get::<i64>(idx)
        .map_err(|e| format!("column {idx}: {e}"))
}

fn optional_integer(row: &Row, idx: i32) -> Result<Option<i64>> {
    row.get::<Option<i64>>(idx)
        .map_err(|e| format!("column {idx}: {e}"))
}

fn row_to_turn(row: &Row) -> Result<Turn> {
    Ok(Turn {
        session_id: text_of(row, 0)?,
        turn_no: integer_of(row, 1)?,
        user_text: text_of(row, 2)?,
        assistant_text: optional_text(row, 3)?,
        user_at: text_of(row, 4)?,
        assistant_at: optional_text(row, 5)?,
    })
}

fn row_to_reply(row: &Row) -> Result<QuestionReply> {
    let options = text_of(row, 5)?;
    Ok(QuestionReply {
        session_id: text_of(row, 0)?,
        turn_no: integer_of(row, 1)?,
        question_id: text_of(row, 2)?,
        question: text_of(row, 3)?,
        answer: text_of(row, 4)?,
        options: serde_json::from_str(&options).map_err(|e| format!("reply options: {e}"))?,
        answered_at: optional_text(row, 6)?,
    })
}

fn row_to_document(row: &Row) -> Result<Document> {
    Ok(Document {
        doc_id: integer_of(row, 0)?,
        name: text_of(row, 1)?,
        kind: text_of(row, 2)?,
        path: text_of(row, 3)?,
        created_at: text_of(row, 4)?,
        updated_at: text_of(row, 5)?,
        body: optional_text(row, 6)?,
        body_hash: optional_text(row, 7)?,
        size: optional_integer(row, 8)?,
    })
}

async fn find_document(conn: &Connection, path: &str) -> Result<Option<Document>> {
    let mut rows = conn
        .query(
            "SELECT doc_id, name, kind, path, created_at, updated_at, body, body_hash, size
             FROM document WHERE path = ?1",
            [path],
        )
        .await
        .map_err(sql_err("find document"))?;
    match rows.next().await.map_err(sql_err("read document"))? {
        Some(row) => row_to_document(&row).map(Some),
        None => Ok(None),
    }
}

/// Paths identify documents, so only one spelling of a path is accepted:
/// absolute, with no `.`/`..` segments, empty segments or line breaks.
fn valid_path(path: &str) -> Result<()> {
    if !path.starts_with('/') {
        return Err(format!(
            "document path must be absolute: {}",
            text::redact(path).0
        ));
    }
    if path.contains('\n') || path.contains('\r') {
        return Err("document path must not contain line breaks".into());
    }
    if path[1..]
        .split('/')
        .any(|seg| matches!(seg, "" | "." | ".."))
    {
        return Err(format!(
            "document path must be normalized: {}",
            text::redact(path).0
        ));
    }
    Ok(())
}

async fn require_project(conn: &Connection, project: &str) -> Result<()> {
    let mut rows = conn
        .query("SELECT 1 FROM project WHERE name = ?1", [project])
        .await
        .map_err(sql_err("find project"))?;
    match rows.next().await.map_err(sql_err("read project"))? {
        Some(_) => Ok(()),
        None => Err(format!("project not found: {project}")),
    }
}

async fn require_session(conn: &Connection, session_id: &str) -> Result<()> {
    let mut rows = conn
        .query("SELECT 1 FROM session WHERE session_id = ?1", [session_id])
        .await
        .map_err(sql_err("find session"))?;
    match rows.next().await.map_err(sql_err("read session"))? {
        Some(_) => Ok(()),
        None => Err(format!("session not found: {session_id}")),
    }
}

async fn require_turn(conn: &Connection, session_id: &str, turn_no: i64) -> Result<()> {
    let mut rows = conn
        .query(
            "SELECT 1 FROM turn WHERE session_id = ?1 AND turn_no = ?2",
            params![session_id, turn_no],
        )
        .await
        .map_err(sql_err("find turn"))?;
    match rows.next().await.map_err(sql_err("read turn"))? {
        Some(_) => Ok(()),
        None => Err(format!("turn not found: {session_id}#{turn_no}")),
    }
}

fn document_name(path: &str) -> Result<&str> {
    Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| format!("document path has no file name: {path}"))
}

struct ChangeInput<'a> {
    doc_id: i64,
    at: &'a str,
    kind: &'a str,
    session_id: Option<&'a str>,
    turn_no: Option<i64>,
    diff: Option<&'a str>,
    hash: Option<&'a str>,
}

async fn append_change(conn: &Connection, change: ChangeInput<'_>) -> Result<()> {
    conn.execute(
        "INSERT INTO document_change
         (doc_id, changed_at, change_kind, session_id, turn_no, diff, body_hash_after)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            change.doc_id,
            change.at,
            change.kind,
            change.session_id,
            change.turn_no,
            change.diff,
            change.hash
        ],
    )
    .await
    .map_err(sql_err("append document change"))?;
    Ok(())
}

async fn index_document(conn: &Connection, doc_id: i64, body: Option<&str>) -> Result<()> {
    conn.execute(
        "DELETE FROM records_fts WHERE kind = 'document' AND doc_id = ?1",
        [doc_id],
    )
    .await
    .map_err(sql_err("remove document index"))?;
    if let Some(body) = body {
        conn.execute(
            "INSERT INTO records_fts (kind, doc_id, tokens) VALUES ('document', ?1, ?2)",
            params![doc_id, text::tokenize(body).join(" ")],
        )
        .await
        .map_err(sql_err("index document"))?;
    }
    Ok(())
}

async fn turn_models(conn: &Connection, session_id: &str, turn_no: i64) -> Result<Vec<TurnModel>> {
    let mut rows = conn
        .query(
            "SELECT session_id, turn_no, model, effort FROM turn_model
             WHERE session_id = ?1 AND turn_no = ?2 ORDER BY model",
            params![session_id, turn_no],
        )
        .await
        .map_err(sql_err("find turn models"))?;
    let mut models = Vec::new();
    while let Some(row) = rows.next().await.map_err(sql_err("read turn models"))? {
        models.push(TurnModel {
            session_id: text_of(&row, 0)?,
            turn_no: integer_of(&row, 1)?,
            model: text_of(&row, 2)?,
            effort: optional_text(&row, 3)?,
        });
    }
    Ok(models)
}

impl Records {
    /// The database file.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Row counts for the health endpoint.
    pub async fn counts(&self) -> Result<Counts> {
        let conn = self.conn.lock().await;
        let mut rows = conn
            .query(
                "SELECT (SELECT count(*) FROM project), (SELECT count(*) FROM session),
                     (SELECT count(*) FROM turn), (SELECT count(*) FROM document),
                     (SELECT count(*) FROM document_change), (SELECT count(*) FROM question_reply)",
                (),
            )
            .await
            .map_err(sql_err("count records"))?;
        let row = rows
            .next()
            .await
            .map_err(sql_err("read counts"))?
            .ok_or("count records: no row")?;
        Ok(Counts {
            projects: integer_of(&row, 0)?,
            sessions: integer_of(&row, 1)?,
            turns: integer_of(&row, 2)?,
            documents: integer_of(&row, 3)?,
            document_changes: integer_of(&row, 4)?,
            replies: integer_of(&row, 5)?,
        })
    }

    /// Opens a local database and initializes the v4 schema.
    pub async fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("create {}: {e}", parent.display()))?;
        }
        let db = Builder::new_local(path)
            .build()
            .await
            .map_err(sql_err("open records database"))?;
        let conn = db.connect().map_err(sql_err("connect"))?;
        conn.execute_batch(
            r#"
            PRAGMA journal_mode = WAL;
            PRAGMA busy_timeout = 5000;
            -- INSERT OR REPLACE deletes the old row; with recursive triggers
            -- on, that delete hits the append-only triggers below.
            PRAGMA recursive_triggers = ON;
            CREATE TABLE IF NOT EXISTS project (
                name TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session (
                session_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                harness TEXT NOT NULL,
                started_at TEXT NOT NULL,
                transcript_path TEXT
            );
            CREATE TABLE IF NOT EXISTS turn (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT,
                user_at TEXT NOT NULL,
                assistant_at TEXT,
                PRIMARY KEY (session_id, turn_no)
            );
            CREATE TRIGGER IF NOT EXISTS turn_append_only_delete BEFORE DELETE ON turn
                BEGIN SELECT RAISE(ABORT, 'turn is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS turn_append_only_update BEFORE UPDATE ON turn
                WHEN NOT (OLD.assistant_text IS NULL AND NEW.assistant_text IS NOT NULL
                    AND NEW.session_id = OLD.session_id AND NEW.turn_no = OLD.turn_no
                    AND NEW.user_text = OLD.user_text AND NEW.user_at = OLD.user_at)
                BEGIN SELECT RAISE(ABORT, 'turn is append-only'); END;
            CREATE TABLE IF NOT EXISTS turn_message (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                text TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (session_id, turn_no, seq)
            );
            CREATE TRIGGER IF NOT EXISTS turn_message_append_only_update
                BEFORE UPDATE ON turn_message
                BEGIN SELECT RAISE(ABORT, 'turn_message is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS turn_message_append_only_delete
                BEFORE DELETE ON turn_message
                BEGIN SELECT RAISE(ABORT, 'turn_message is append-only'); END;
            CREATE TABLE IF NOT EXISTS turn_model (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                model TEXT NOT NULL,
                effort TEXT,
                PRIMARY KEY (session_id, turn_no, model)
            );
            CREATE TABLE IF NOT EXISTS document (
                doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                body TEXT,
                body_hash TEXT,
                size INTEGER
            );
            CREATE TABLE IF NOT EXISTS document_change (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                change_kind TEXT NOT NULL CHECK (change_kind IN ('create', 'modify', 'rename', 'delete')),
                session_id TEXT,
                turn_no INTEGER,
                diff TEXT,
                body_hash_after TEXT
            );
            CREATE TRIGGER IF NOT EXISTS document_change_append_only_update
                BEFORE UPDATE ON document_change
                BEGIN SELECT RAISE(ABORT, 'document_change is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS document_change_append_only_delete
                BEFORE DELETE ON document_change
                BEGIN SELECT RAISE(ABORT, 'document_change is append-only'); END;
            CREATE TABLE IF NOT EXISTS question_reply (
                session_id TEXT NOT NULL,
                turn_no INTEGER NOT NULL,
                question_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                options TEXT NOT NULL CHECK (json_valid(options) AND json_type(options) = 'array'),
                answered_at TEXT,
                PRIMARY KEY (session_id, turn_no, question_id)
            );
            CREATE TRIGGER IF NOT EXISTS question_reply_append_only_update
                BEFORE UPDATE ON question_reply
                BEGIN SELECT RAISE(ABORT, 'question_reply is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS question_reply_append_only_delete
                BEFORE DELETE ON question_reply
                BEGIN SELECT RAISE(ABORT, 'question_reply is append-only'); END;
            CREATE TABLE IF NOT EXISTS project_document (
                project TEXT NOT NULL,
                doc_id INTEGER NOT NULL,
                PRIMARY KEY (project, doc_id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
                kind UNINDEXED, session_id UNINDEXED, turn_no UNINDEXED, doc_id UNINDEXED,
                question_id UNINDEXED, tokens, tokenize = "unicode61 tokenchars '._-'"
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema_version', '5')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            "#,
        )
        .await
        .map_err(sql_err("initialize records schema"))?;
        Ok(Self {
            _db: db,
            conn: Mutex::new(conn),
            path: path.to_path_buf(),
        })
    }

    /// Registers a project, preserving its original creation time.
    pub async fn upsert_project(&self, name: &str, root_path: &str) -> Result<()> {
        let now = time::OffsetDateTime::now_utc();
        let at = now
            .replace_nanosecond(0)
            .unwrap_or(now)
            .format(&time::format_description::well_known::Rfc3339)
            .map_err(|e| format!("project timestamp: {e}"))?;
        self.conn
            .lock()
            .await
            .execute(
                "INSERT INTO project (name, root_path, created_at) VALUES (?1, ?2, ?3)
                 ON CONFLICT(name) DO UPDATE SET root_path = excluded.root_path",
                params![name, root_path, at],
            )
            .await
            .map_err(sql_err("upsert project"))?;
        Ok(())
    }

    /// Registers a session only when its project exists.
    pub async fn upsert_session(&self, session: &Session) -> Result<()> {
        let conn = self.conn.lock().await;
        let tx = conn.transaction().await.map_err(sql_err("begin session"))?;
        let mut rows = tx
            .query(
                "SELECT 1 FROM project WHERE name = ?1",
                [session.project.as_str()],
            )
            .await
            .map_err(sql_err("find project"))?;
        if rows
            .next()
            .await
            .map_err(sql_err("read project"))?
            .is_none()
        {
            return Err(format!("project not found: {}", session.project));
        }
        drop(rows);
        // A session's turns carry only its id, so moving it to another
        // project (or harness) would silently move every record with it.
        let mut rows = tx
            .query(
                "SELECT project, harness FROM session WHERE session_id = ?1",
                [session.session_id.as_str()],
            )
            .await
            .map_err(sql_err("find session"))?;
        if let Some(row) = rows.next().await.map_err(sql_err("read session"))? {
            let (project, harness) = (text_of(&row, 0)?, text_of(&row, 1)?);
            if project != session.project || harness != session.harness {
                return Err(format!(
                    "session {} already belongs to project '{project}' on {harness}",
                    session.session_id
                ));
            }
        }
        drop(rows);
        tx.execute(
            "INSERT INTO session (session_id, project, harness, started_at, transcript_path)
             VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT(session_id) DO UPDATE SET started_at = excluded.started_at,
                 transcript_path = excluded.transcript_path",
            params![
                session.session_id.as_str(),
                session.project.as_str(),
                session.harness.as_str(),
                session.started_at.as_str(),
                session.transcript_path.as_deref()
            ],
        )
        .await
        .map_err(sql_err("upsert session"))?;
        tx.commit().await.map_err(sql_err("commit session"))?;
        Ok(())
    }

    /// Appends a redacted turn, or fills its previously absent answer once.
    pub async fn record_turn(&self, turn: &Turn) -> Result<RecordOutcome> {
        let (user, mut redacted) = text::redact(&turn.user_text);
        let assistant = turn.assistant_text.as_deref().map(|body| {
            let (body, count) = text::redact(body);
            redacted += count;
            body
        });
        let conn = self.conn.lock().await;
        let tx = conn.transaction().await.map_err(sql_err("begin turn"))?;
        require_session(&tx, &turn.session_id).await?;
        let mut rows = tx
            .query(
                "SELECT session_id, turn_no, user_text, assistant_text, user_at, assistant_at
                 FROM turn WHERE session_id = ?1 AND turn_no = ?2",
                params![turn.session_id.as_str(), turn.turn_no],
            )
            .await
            .map_err(sql_err("find turn"))?;
        let existing = match rows.next().await.map_err(sql_err("read turn"))? {
            Some(row) => Some(row_to_turn(&row)?),
            None => None,
        };
        drop(rows);
        if let Some(old) = existing {
            if old.user_text == user
                && old.assistant_text == assistant
                && old.user_at == turn.user_at
                && old.assistant_at == turn.assistant_at
            {
                tx.commit().await.map_err(sql_err("commit turn"))?;
                return Ok(RecordOutcome {
                    inserted: false,
                    redacted,
                });
            }
            if old.user_text == user && old.user_at == turn.user_at && assistant.is_none() {
                // The answerless version arriving after the answered one is
                // a retransmission, not a conflict.
                tx.commit().await.map_err(sql_err("commit turn"))?;
                return Ok(RecordOutcome {
                    inserted: false,
                    redacted,
                });
            }
            if old.assistant_text.is_some() || old.user_text != user || old.user_at != turn.user_at
            {
                return Err(format!(
                    "turn {}#{} already recorded",
                    turn.session_id, turn.turn_no
                ));
            }
            tx.execute(
                "UPDATE turn SET assistant_text = ?3, assistant_at = ?4
                 WHERE session_id = ?1 AND turn_no = ?2",
                params![
                    turn.session_id.as_str(),
                    turn.turn_no,
                    assistant.as_deref(),
                    turn.assistant_at.as_deref()
                ],
            )
            .await
            .map_err(sql_err("fill turn answer"))?;
        } else {
            tx.execute(
                "INSERT INTO turn (session_id, turn_no, user_text, assistant_text, user_at, assistant_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![turn.session_id.as_str(), turn.turn_no, user.as_str(), assistant.as_deref(), turn.user_at.as_str(), turn.assistant_at.as_deref()],
            )
            .await
            .map_err(sql_err("insert turn"))?;
        }
        tx.execute(
            "DELETE FROM records_fts WHERE kind = 'turn' AND session_id = ?1 AND turn_no = ?2",
            params![turn.session_id.as_str(), turn.turn_no],
        )
        .await
        .map_err(sql_err("remove turn index"))?;
        let tokens =
            text::tokenize(&format!("{user} {}", assistant.as_deref().unwrap_or(""))).join(" ");
        tx.execute(
            "INSERT INTO records_fts (kind, session_id, turn_no, tokens) VALUES ('turn', ?1, ?2, ?3)",
            params![turn.session_id.as_str(), turn.turn_no, tokens],
        )
        .await
        .map_err(sql_err("index turn"))?;
        tx.commit().await.map_err(sql_err("commit turn"))?;
        Ok(RecordOutcome {
            inserted: true,
            redacted,
        })
    }

    /// Appends a redacted added message, accepting only identical retransmissions.
    pub async fn record_turn_message(&self, message: &TurnMessage) -> Result<RecordOutcome> {
        let (body, redacted) = text::redact(&message.text);
        let conn = self.conn.lock().await;
        let tx = conn
            .transaction()
            .await
            .map_err(sql_err("begin turn message"))?;
        require_turn(&tx, &message.session_id, message.turn_no).await?;
        let mut rows = tx
            .query(
                "SELECT text, sent_at FROM turn_message
                 WHERE session_id = ?1 AND turn_no = ?2 AND seq = ?3",
                params![message.session_id.as_str(), message.turn_no, message.seq],
            )
            .await
            .map_err(sql_err("find turn message"))?;
        let existing = match rows.next().await.map_err(sql_err("read turn message"))? {
            Some(row) => Some((text_of(&row, 0)?, text_of(&row, 1)?)),
            None => None,
        };
        drop(rows);
        if let Some((old_text, old_at)) = existing {
            if old_text != body || old_at != message.sent_at {
                return Err(format!(
                    "turn message {}#{}.{} already recorded",
                    message.session_id, message.turn_no, message.seq
                ));
            }
            tx.commit().await.map_err(sql_err("commit turn message"))?;
            return Ok(RecordOutcome {
                inserted: false,
                redacted,
            });
        }
        tx.execute(
            "INSERT INTO turn_message (session_id, turn_no, seq, text, sent_at)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                message.session_id.as_str(),
                message.turn_no,
                message.seq,
                body.as_str(),
                message.sent_at.as_str()
            ],
        )
        .await
        .map_err(sql_err("insert turn message"))?;
        tx.execute(
            "INSERT INTO records_fts (kind, session_id, turn_no, question_id, tokens)
             VALUES ('message', ?1, ?2, ?3, ?4)",
            params![
                message.session_id.as_str(),
                message.turn_no,
                message.seq.to_string(),
                text::tokenize(&body).join(" ")
            ],
        )
        .await
        .map_err(sql_err("index turn message"))?;
        tx.commit().await.map_err(sql_err("commit turn message"))?;
        Ok(RecordOutcome {
            inserted: true,
            redacted,
        })
    }

    /// Adds a model once per turn and model name.
    pub async fn record_turn_model(&self, model: &TurnModel) -> Result<RecordOutcome> {
        let conn = self.conn.lock().await;
        let tx = conn
            .transaction()
            .await
            .map_err(sql_err("begin turn model"))?;
        require_turn(&tx, &model.session_id, model.turn_no).await?;
        let inserted = tx.execute(
            "INSERT INTO turn_model (session_id, turn_no, model, effort) VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(session_id, turn_no, model) DO NOTHING",
            params![model.session_id.as_str(), model.turn_no, model.model.as_str(), model.effort.as_deref()],
        ).await.map_err(sql_err("record turn model"))?;
        tx.commit().await.map_err(sql_err("commit turn model"))?;
        Ok(RecordOutcome {
            inserted: inserted > 0,
            redacted: 0,
        })
    }

    /// Records a snapshot, its diff, project association, and search index atomically.
    pub async fn record_document(&self, input: &DocumentInput) -> Result<DocumentOutcome> {
        valid_path(&input.path)?;
        let name = document_name(&input.path)?;
        match (&input.content, input.kind.as_str()) {
            (Content::Text(_), "artifact") => {
                return Err("an artifact carries a hash and size, not text".into());
            }
            (Content::Binary { .. }, kind) if kind != "artifact" => {
                return Err(format!(
                    "binary content must be kind 'artifact', not '{kind}'"
                ));
            }
            _ => {}
        }
        let (body, hash, size) = match &input.content {
            Content::Text(body) => {
                let (body, _) = text::redact(body);
                let hash = format!("{:x}", Sha256::digest(body.as_bytes()));
                let size = i64::try_from(body.len()).map_err(|e| format!("document size: {e}"))?;
                (Some(body), Cow::Owned(hash), size)
            }
            Content::Binary { hash, size } => (None, Cow::Borrowed(hash.as_str()), *size),
        };
        let conn = self.conn.lock().await;
        let tx = conn
            .transaction()
            .await
            .map_err(sql_err("begin document"))?;
        let existing = find_document(&tx, &input.path).await?;
        let mut deleted = false;
        if let Some(old) = &existing {
            let mut rows = tx.query(
                "SELECT change_kind FROM document_change WHERE doc_id = ?1 ORDER BY change_id DESC LIMIT 1",
                [old.doc_id],
            ).await.map_err(sql_err("find last document change"))?;
            if let Some(row) = rows
                .next()
                .await
                .map_err(sql_err("read last document change"))?
            {
                deleted = text_of(&row, 0)? == "delete";
            }
        }
        let change = if existing.is_none() || deleted {
            Some("create")
        } else if existing.as_ref().is_some_and(|d| {
            d.body_hash.as_deref() == Some(hash.as_ref())
                && d.kind == input.kind
                && d.body.is_some() == body.is_some()
                && d.size == Some(size)
        }) {
            None
        } else {
            Some("modify")
        };
        let doc_id = if let Some(old) = &existing {
            old.doc_id
        } else {
            tx.execute(
                "INSERT INTO document (name, kind, path, created_at, updated_at, body, body_hash, size)
                 VALUES (?1, ?2, ?3, ?4, ?4, ?5, ?6, ?7)",
                params![name, input.kind.as_str(), input.path.as_str(), input.at.as_str(), body.as_deref(), hash.as_ref(), size],
            ).await.map_err(sql_err("create document"))?;
            tx.last_insert_rowid()
        };
        if let Some(project) = input.project.as_deref() {
            require_project(&tx, project).await?;
            tx.execute(
                "INSERT INTO project_document (project, doc_id) VALUES (?1, ?2)
                 ON CONFLICT(project, doc_id) DO NOTHING",
                params![project, doc_id],
            )
            .await
            .map_err(sql_err("link project document"))?;
        }
        if let Some(kind) = change {
            if existing.is_some() {
                tx.execute(
                    "UPDATE document SET name = ?2, kind = ?3, updated_at = ?4,
                     body = ?5, body_hash = ?6, size = ?7 WHERE doc_id = ?1",
                    params![
                        doc_id,
                        name,
                        input.kind.as_str(),
                        input.at.as_str(),
                        body.as_deref(),
                        hash.as_ref(),
                        size
                    ],
                )
                .await
                .map_err(sql_err("update document"))?;
            }
            let diff = body.as_deref().map(|new| {
                let old = if kind == "create" {
                    ""
                } else {
                    existing
                        .as_ref()
                        .and_then(|d| d.body.as_deref())
                        .unwrap_or("")
                };
                // The header is display text: a secret in a file name must
                // not survive there when the body is already clean.
                let (shown, _) = text::redact(&input.path);
                TextDiff::from_lines(old, new)
                    .unified_diff()
                    .context_radius(3)
                    .header(&shown, &shown)
                    .to_string()
            });
            append_change(
                &tx,
                ChangeInput {
                    doc_id,
                    at: &input.at,
                    kind,
                    session_id: input.session_id.as_deref(),
                    turn_no: input.turn_no,
                    diff: diff.as_deref(),
                    hash: Some(hash.as_ref()),
                },
            )
            .await?;
            index_document(&tx, doc_id, body.as_deref()).await?;
        }
        tx.commit().await.map_err(sql_err("commit document"))?;
        Ok(DocumentOutcome {
            doc_id,
            change: change.map(String::from),
        })
    }

    /// Renames a document without losing its identity or history. A resend
    /// whose source is gone and whose target exists changes nothing. When the
    /// target path belongs to a deleted document, that row keeps its history:
    /// the content moves into it and both rows record the rename.
    pub async fn rename_document(
        &self,
        old_path: &str,
        new_path: &str,
        at: &str,
        session_id: Option<&str>,
        turn_no: Option<i64>,
    ) -> Result<DocumentOutcome> {
        valid_path(new_path)?;
        let name = document_name(new_path)?;
        let conn = self.conn.lock().await;
        let tx = conn.transaction().await.map_err(sql_err("begin rename"))?;
        let target = find_document(&tx, new_path).await?;
        let Some(doc) = find_document(&tx, old_path).await? else {
            return match target {
                // Already applied: the adapter is resending after a lost reply.
                Some(target) => Ok(DocumentOutcome {
                    doc_id: target.doc_id,
                    change: None,
                }),
                None => Err(format!("document not found: {}", text::redact(old_path).0)),
            };
        };
        let diff = format!(
            "--- {}\n+++ {}\n",
            text::redact(old_path).0,
            text::redact(new_path).0
        );
        let doc_id = match target {
            Some(target) if target.body_hash.is_some() => {
                return Err(format!(
                    "document already exists: {}",
                    text::redact(new_path).0
                ));
            }
            Some(target) => {
                tx.execute(
                    "UPDATE document SET name = ?2, kind = ?3, updated_at = ?4,
                         body = ?5, body_hash = ?6, size = ?7
                     WHERE doc_id = ?1",
                    params![
                        target.doc_id,
                        name,
                        doc.kind.as_str(),
                        at,
                        doc.body.as_deref(),
                        doc.body_hash.as_deref(),
                        doc.size
                    ],
                )
                .await
                .map_err(sql_err("revive document"))?;
                tx.execute(
                    "UPDATE document SET body = NULL, body_hash = NULL, size = NULL, updated_at = ?2
                     WHERE doc_id = ?1",
                    params![doc.doc_id, at],
                )
                .await
                .map_err(sql_err("clear renamed document"))?;
                tx.execute(
                    "INSERT INTO project_document (project, doc_id)
                     SELECT project, ?2 FROM project_document WHERE doc_id = ?1
                     ON CONFLICT(project, doc_id) DO NOTHING",
                    params![doc.doc_id, target.doc_id],
                )
                .await
                .map_err(sql_err("link renamed document"))?;
                append_change(
                    &tx,
                    ChangeInput {
                        doc_id: doc.doc_id,
                        at,
                        kind: "rename",
                        session_id,
                        turn_no,
                        diff: Some(&diff),
                        hash: None,
                    },
                )
                .await?;
                index_document(&tx, doc.doc_id, None).await?;
                index_document(&tx, target.doc_id, doc.body.as_deref()).await?;
                target.doc_id
            }
            None => {
                tx.execute(
                    "UPDATE document SET path = ?2, name = ?3, updated_at = ?4 WHERE doc_id = ?1",
                    params![doc.doc_id, new_path, name, at],
                )
                .await
                .map_err(sql_err("rename document"))?;
                doc.doc_id
            }
        };
        append_change(
            &tx,
            ChangeInput {
                doc_id,
                at,
                kind: "rename",
                session_id,
                turn_no,
                diff: Some(&diff),
                hash: doc.body_hash.as_deref(),
            },
        )
        .await?;
        tx.commit().await.map_err(sql_err("commit rename"))?;
        Ok(DocumentOutcome {
            doc_id,
            change: Some("rename".into()),
        })
    }

    /// Clears a snapshot and its index while retaining its row and history.
    pub async fn delete_document(
        &self,
        path: &str,
        at: &str,
        session_id: Option<&str>,
        turn_no: Option<i64>,
    ) -> Result<DocumentOutcome> {
        let conn = self.conn.lock().await;
        let tx = conn.transaction().await.map_err(sql_err("begin delete"))?;
        let doc = find_document(&tx, path)
            .await?
            .ok_or_else(|| format!("document not found: {}", text::redact(path).0))?;
        if doc.body_hash.is_none() {
            // Already deleted: the adapter is resending after a lost reply.
            return Ok(DocumentOutcome {
                doc_id: doc.doc_id,
                change: None,
            });
        }
        tx.execute(
            "UPDATE document SET body = NULL, body_hash = NULL, size = NULL, updated_at = ?2
             WHERE doc_id = ?1",
            params![doc.doc_id, at],
        )
        .await
        .map_err(sql_err("delete document"))?;
        append_change(
            &tx,
            ChangeInput {
                doc_id: doc.doc_id,
                at,
                kind: "delete",
                session_id,
                turn_no,
                diff: None,
                hash: None,
            },
        )
        .await?;
        index_document(&tx, doc.doc_id, None).await?;
        tx.commit().await.map_err(sql_err("commit delete"))?;
        Ok(DocumentOutcome {
            doc_id: doc.doc_id,
            change: Some("delete".into()),
        })
    }

    /// Appends a redacted reply, accepting only identical retransmissions.
    pub async fn record_question_reply(&self, reply: &QuestionReply) -> Result<RecordOutcome> {
        let (question, mut redacted) = text::redact(&reply.question);
        let (answer, count) = text::redact(&reply.answer);
        redacted += count;
        let options: Vec<String> = reply
            .options
            .iter()
            .map(|option| {
                let (clean, count) = text::redact(option);
                redacted += count;
                clean
            })
            .collect();
        let options_json =
            serde_json::to_string(&options).map_err(|e| format!("reply options: {e}"))?;
        let conn = self.conn.lock().await;
        let tx = conn.transaction().await.map_err(sql_err("begin reply"))?;
        require_turn(&tx, &reply.session_id, reply.turn_no).await?;
        let mut rows = tx
            .query(
                "SELECT session_id, turn_no, question_id, question, answer, options, answered_at
             FROM question_reply WHERE session_id = ?1 AND turn_no = ?2 AND question_id = ?3",
                params![
                    reply.session_id.as_str(),
                    reply.turn_no,
                    reply.question_id.as_str()
                ],
            )
            .await
            .map_err(sql_err("find reply"))?;
        if let Some(row) = rows.next().await.map_err(sql_err("read reply"))? {
            let old = row_to_reply(&row)?;
            if old.question != question
                || old.answer != answer
                || old.options != options
                || old.answered_at != reply.answered_at
            {
                return Err(format!(
                    "question reply {} already recorded",
                    reply.question_id
                ));
            }
            drop(rows);
            tx.commit().await.map_err(sql_err("commit reply"))?;
            return Ok(RecordOutcome {
                inserted: false,
                redacted,
            });
        }
        drop(rows);
        tx.execute(
            "INSERT INTO question_reply (session_id, turn_no, question_id, question, answer, options, answered_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![reply.session_id.as_str(), reply.turn_no, reply.question_id.as_str(), question.as_str(), answer.as_str(), options_json, reply.answered_at.as_deref()],
        ).await.map_err(sql_err("insert reply"))?;
        let tokens = text::tokenize(&format!("{question} {answer}")).join(" ");
        tx.execute(
            "INSERT INTO records_fts (kind, session_id, turn_no, question_id, tokens)
             VALUES ('reply', ?1, ?2, ?3, ?4)",
            params![
                reply.session_id.as_str(),
                reply.turn_no,
                reply.question_id.as_str(),
                tokens
            ],
        )
        .await
        .map_err(sql_err("index reply"))?;
        tx.commit().await.map_err(sql_err("commit reply"))?;
        Ok(RecordOutcome {
            inserted: true,
            redacted,
        })
    }

    /// Searches lexical candidates, then filters by kind and project.
    pub async fn search(
        &self,
        query: &str,
        scope: Scope,
        project: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Hit>> {
        let tokens = text::tokenize(query);
        if tokens.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        let expression = tokens
            .iter()
            .map(|token| format!("\"{}\"", token.replace('"', "")))
            .collect::<Vec<_>>()
            .join(" OR ");
        let kind = match scope {
            Scope::All => None,
            Scope::Turns => Some("turn"),
            Scope::Documents => Some("document"),
            Scope::Replies => Some("reply"),
        };
        let conn = self.conn.lock().await;
        let mut rows = conn.query(
            "WITH candidates AS MATERIALIZED (
                 SELECT rowid AS fts_id, kind, session_id, turn_no, doc_id, question_id,
                     bm25(records_fts) AS rank
                 FROM records_fts WHERE records_fts MATCH ?1
                 ORDER BY bm25(records_fts), rowid LIMIT ?2
             )
             SELECT f.kind, f.session_id, f.turn_no, f.doc_id, d.path,
                 CASE f.kind
                     WHEN 'turn' THEN replace(replace(t.user_text, char(13), ' '), char(10), ' ')
                         || CASE WHEN t.assistant_text IS NULL THEN '' ELSE char(10) || t.assistant_text END
                     WHEN 'document' THEN COALESCE(d.body, '')
                     WHEN 'reply' THEN q.question || ' — ' || q.answer
                     WHEN 'message' THEN m.text
                 END, f.rank
             FROM candidates f
             LEFT JOIN turn t ON f.kind = 'turn' AND t.session_id = f.session_id AND t.turn_no = f.turn_no
             LEFT JOIN document d ON f.kind = 'document' AND d.doc_id = f.doc_id
             LEFT JOIN question_reply q ON f.kind = 'reply' AND q.session_id = f.session_id
                 AND q.turn_no = f.turn_no AND q.question_id = f.question_id
             LEFT JOIN turn_message m ON f.kind = 'message' AND m.session_id = f.session_id
                 AND m.turn_no = f.turn_no AND CAST(m.seq AS TEXT) = f.question_id
             WHERE (?3 IS NULL OR f.kind = ?3 OR (?3 = 'turn' AND f.kind = 'message'))
                 AND (f.kind != 'turn' OR t.session_id IS NOT NULL)
                 AND (f.kind != 'reply' OR q.session_id IS NOT NULL)
                 AND (f.kind != 'message' OR m.session_id IS NOT NULL)
                 AND (f.kind != 'document' OR d.body IS NOT NULL)
                 AND (?4 IS NULL OR
                     (f.kind IN ('turn', 'reply', 'message') AND EXISTS (
                         SELECT 1 FROM session s WHERE s.session_id = f.session_id AND s.project = ?4))
                     OR (f.kind = 'document' AND EXISTS (
                         SELECT 1 FROM project_document p WHERE p.doc_id = f.doc_id AND p.project = ?4)))
             ORDER BY f.rank, f.fts_id",
            // Scope and project are filtered after the match, so the
            // candidate cap is a sanity bound, not a page size.
            params![expression, 5000_i64, kind, project],
        ).await.map_err(sql_err("search records"))?;
        let mut hits = Vec::new();
        while hits.len() < limit {
            let Some(row) = rows.next().await.map_err(sql_err("read search hits"))? else {
                break;
            };
            hits.push(Hit {
                kind: text_of(&row, 0)?,
                session_id: optional_text(&row, 1)?,
                turn_no: optional_integer(&row, 2)?,
                doc_id: optional_integer(&row, 3)?,
                path: optional_text(&row, 4)?,
                excerpt: text_of(&row, 5)?.chars().take(200).collect(),
                score: -row.get::<f64>(6).map_err(sql_err("read search rank"))?,
                models: Vec::new(),
            });
        }
        drop(rows);
        for hit in &mut hits {
            if let (Some(session_id), Some(turn_no)) = (hit.session_id.as_deref(), hit.turn_no) {
                hit.models = turn_models(&conn, session_id, turn_no).await?;
            }
        }
        Ok(hits)
    }

    /// Returns recent turns with all attached records in stable order.
    pub async fn recent(
        &self,
        project: Option<&str>,
        session_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<RecentTurn>> {
        let conn = self.conn.lock().await;
        let mut rows = conn.query(
            &format!(
                "SELECT t.session_id, t.turn_no, t.user_text, t.assistant_text, t.user_at, t.assistant_at
                 FROM turn t JOIN session s ON s.session_id = t.session_id
                 WHERE (?1 IS NULL OR s.project = ?1) AND (?2 IS NULL OR t.session_id = ?2)
                 ORDER BY t.user_at DESC, t.session_id ASC, t.turn_no DESC LIMIT {}", limit
            ),
            params![project, session_id],
        ).await.map_err(sql_err("find recent turns"))?;
        let mut turns = Vec::new();
        while let Some(row) = rows.next().await.map_err(sql_err("read recent turns"))? {
            turns.push(row_to_turn(&row)?);
        }
        drop(rows);
        let mut recent = Vec::new();
        for turn in turns {
            let models = turn_models(&conn, &turn.session_id, turn.turn_no).await?;
            let mut rows = conn
                .query(
                    "SELECT seq, text, sent_at FROM turn_message
                     WHERE session_id = ?1 AND turn_no = ?2 ORDER BY seq",
                    params![turn.session_id.as_str(), turn.turn_no],
                )
                .await
                .map_err(sql_err("find turn messages"))?;
            let mut messages = Vec::new();
            while let Some(row) = rows.next().await.map_err(sql_err("read turn messages"))? {
                messages.push(TurnMessage {
                    session_id: turn.session_id.clone(),
                    turn_no: turn.turn_no,
                    seq: integer_of(&row, 0)?,
                    text: text_of(&row, 1)?,
                    sent_at: text_of(&row, 2)?,
                });
            }
            let mut rows = conn.query(
                "SELECT session_id, turn_no, question_id, question, answer, options, answered_at
                 FROM question_reply WHERE session_id = ?1 AND turn_no = ?2 ORDER BY question_id",
                params![turn.session_id.as_str(), turn.turn_no],
            ).await.map_err(sql_err("find turn replies"))?;
            let mut replies = Vec::new();
            while let Some(row) = rows.next().await.map_err(sql_err("read turn replies"))? {
                replies.push(row_to_reply(&row)?);
            }
            let mut rows = conn.query(
                "SELECT DISTINCT d.path FROM document_change c JOIN document d ON d.doc_id = c.doc_id
                 WHERE c.session_id = ?1 AND c.turn_no = ?2 ORDER BY d.path",
                params![turn.session_id.as_str(), turn.turn_no],
            ).await.map_err(sql_err("find changed documents"))?;
            let mut changed_documents = Vec::new();
            while let Some(row) = rows
                .next()
                .await
                .map_err(sql_err("read changed documents"))?
            {
                changed_documents.push(text_of(&row, 0)?);
            }
            recent.push(RecentTurn {
                turn,
                messages,
                models,
                replies,
                changed_documents,
            });
        }
        Ok(recent)
    }

    /// Returns a document's immutable changes, newest first.
    pub async fn history(&self, path: &str, limit: usize) -> Result<Vec<DocumentChange>> {
        let conn = self.conn.lock().await;
        let mut rows = conn
            .query(
                &format!(
                    "SELECT c.change_id, c.doc_id, c.changed_at, c.change_kind, c.session_id,
                     c.turn_no, c.diff, c.body_hash_after
                 FROM document_change c JOIN document d ON d.doc_id = c.doc_id
                 WHERE d.path = ?1 ORDER BY c.change_id DESC LIMIT {}",
                    limit
                ),
                [path],
            )
            .await
            .map_err(sql_err("find document history"))?;
        let mut history = Vec::new();
        while let Some(row) = rows
            .next()
            .await
            .map_err(sql_err("read document history"))?
        {
            history.push(DocumentChange {
                change_id: integer_of(&row, 0)?,
                doc_id: integer_of(&row, 1)?,
                changed_at: text_of(&row, 2)?,
                change_kind: text_of(&row, 3)?,
                session_id: optional_text(&row, 4)?,
                turn_no: optional_integer(&row, 5)?,
                diff: optional_text(&row, 6)?,
                body_hash_after: optional_text(&row, 7)?,
            });
        }
        Ok(history)
    }

    /// Finds the last snapshot at a path, including cleared deleted rows.
    pub async fn document(&self, path: &str) -> Result<Option<Document>> {
        let conn = self.conn.lock().await;
        find_document(&conn, path).await
    }

    /// Executes one SQL statement; tests use it to probe the triggers.
    pub async fn raw_exec(&self, sql: &str) -> Result<u64> {
        let conn = self.conn.lock().await;
        conn.execute(sql, ()).await.map_err(sql_err("raw SQL"))
    }
}
