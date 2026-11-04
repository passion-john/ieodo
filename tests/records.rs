//! The v0.4 record store (wiki/schema.md): projects, sessions, user/assistant
//! turn pairs, the model behind each turn, documents with unified-diff
//! history, question replies, and one FTS index over all of it.

use ieodo::records::{
    Content, DocumentInput, QuestionReply, Records, Scope, Session, Turn, TurnModel,
};

async fn open(dir: &tempfile::TempDir) -> Records {
    Records::open(&dir.path().join("records.db")).await.unwrap()
}

fn session(id: &str, project: &str) -> Session {
    Session {
        session_id: id.into(),
        project: project.into(),
        harness: "claude-code".into(),
        started_at: "2026-10-27T01:00:00Z".into(),
        transcript_path: Some(format!("/tmp/{id}.jsonl")),
    }
}

fn turn(session_id: &str, no: i64, user: &str, assistant: Option<&str>) -> Turn {
    Turn {
        session_id: session_id.into(),
        turn_no: no,
        user_text: user.into(),
        assistant_text: assistant.map(str::to_owned),
        user_at: format!("2026-10-27T01:{no:02}:00Z"),
        assistant_at: assistant.map(|_| format!("2026-10-27T01:{no:02}:30Z")),
    }
}

fn text_doc(path: &str, body: &str, at: &str, origin: Option<(&str, i64)>) -> DocumentInput {
    DocumentInput {
        path: path.into(),
        kind: "record".into(),
        content: Content::Text(body.into()),
        at: at.into(),
        session_id: origin.map(|(s, _)| s.to_owned()),
        turn_no: origin.map(|(_, n)| n),
        project: Some("demo".into()),
    }
}

async fn seeded(dir: &tempfile::TempDir) -> Records {
    let r = open(dir).await;
    r.upsert_project("demo", "/work/demo").await.unwrap();
    r.upsert_session(&session("s1", "demo")).await.unwrap();
    r.record_turn(&turn(
        "s1",
        1,
        "libSQL은 SQLite 포크야?",
        Some("맞아, libSQL은 SQLite의 포크야."),
    ))
    .await
    .unwrap();
    r
}

#[tokio::test]
async fn turns_are_append_only_except_filling_the_answer_once() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;

    // Same key, same content: a no-op, not an error.
    let again = r
        .record_turn(&turn(
            "s1",
            1,
            "libSQL은 SQLite 포크야?",
            Some("맞아, libSQL은 SQLite의 포크야."),
        ))
        .await
        .unwrap();
    assert!(!again.inserted);

    // Same key, different content: refused.
    let err = r
        .record_turn(&turn("s1", 1, "다른 말", Some("다른 답")))
        .await
        .unwrap_err();
    assert!(err.contains("already recorded"), "{err}");

    // A turn recorded before the answer arrived may receive the answer once.
    r.record_turn(&turn("s1", 2, "두 번째 질문", None))
        .await
        .unwrap();
    r.record_turn(&turn("s1", 2, "두 번째 질문", Some("두 번째 답")))
        .await
        .unwrap();
    let recent = r.recent(Some("demo"), None, 10).await.unwrap();
    let second = recent.iter().find(|t| t.turn.turn_no == 2).unwrap();
    assert_eq!(second.turn.assistant_text.as_deref(), Some("두 번째 답"));
    // But not a different answer afterwards.
    assert!(
        r.record_turn(&turn("s1", 2, "두 번째 질문", Some("바뀐 답")))
            .await
            .is_err()
    );

    // The database itself refuses edits and deletes (on rows that exist, so
    // the row triggers actually fire).
    r.record_document(&text_doc(
        "/work/demo/x.md",
        "x\n",
        "2026-10-27T01:01:40Z",
        Some(("s1", 1)),
    ))
    .await
    .unwrap();
    r.record_question_reply(&QuestionReply {
        session_id: "s1".into(),
        turn_no: 1,
        question_id: "q0".into(),
        question: "계속할까?".into(),
        answer: "응".into(),
        options: vec!["응".into(), "아니".into()],
        answered_at: None,
    })
    .await
    .unwrap();
    assert!(
        r.raw_exec("UPDATE turn SET user_text = 'x' WHERE turn_no = 1")
            .await
            .unwrap_err()
            .contains("append-only")
    );
    assert!(
        r.raw_exec("DELETE FROM turn WHERE turn_no = 1")
            .await
            .unwrap_err()
            .contains("append-only")
    );
    assert!(
        r.raw_exec("DELETE FROM document_change")
            .await
            .unwrap_err()
            .contains("append-only")
    );
    assert!(
        r.raw_exec("DELETE FROM question_reply")
            .await
            .unwrap_err()
            .contains("append-only")
    );
    for sql in [
        "UPDATE document_change SET diff = 'x'",
        "UPDATE question_reply SET answer = 'x'",
        // REPLACE deletes the old row first; that delete must be refused too.
        "INSERT OR REPLACE INTO turn (session_id, turn_no, user_text, user_at) VALUES ('s1', 1, 'x', 'y')",
    ] {
        let err = r.raw_exec(sql).await.unwrap_err();
        assert!(err.contains("append-only"), "{sql}: {err}");
    }
    // An answerless row may only ever receive an answer, nothing else.
    r.record_turn(&turn("s1", 4, "답 없는 질문", None))
        .await
        .unwrap();
    assert!(
        r.raw_exec("UPDATE turn SET assistant_at = 'x' WHERE turn_no = 4")
            .await
            .unwrap_err()
            .contains("append-only")
    );
    assert_eq!(r.recent(None, Some("s1"), 10).await.unwrap().len(), 3);
}

#[tokio::test]
async fn records_need_their_parents_and_sessions_stay_in_their_project() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    r.upsert_project("other", "/work/other").await.unwrap();
    let moved = Session {
        project: "other".into(),
        ..session("s1", "demo")
    };
    assert!(
        r.upsert_session(&moved)
            .await
            .unwrap_err()
            .contains("already")
    );
    // Re-registering the same session may update its paths and time.
    let later = Session {
        transcript_path: Some("/tmp/s1-moved.jsonl".into()),
        ..session("s1", "demo")
    };
    r.upsert_session(&later).await.unwrap();

    assert!(
        r.record_turn(&turn("ghost", 1, "x", None))
            .await
            .unwrap_err()
            .contains("session")
    );
    assert!(
        r.record_turn_model(&TurnModel {
            session_id: "s1".into(),
            turn_no: 99,
            model: "m".into(),
            effort: None,
        })
        .await
        .unwrap_err()
        .contains("turn")
    );
    assert!(
        r.record_question_reply(&QuestionReply {
            session_id: "s1".into(),
            turn_no: 99,
            question_id: "q".into(),
            question: "?".into(),
            answer: "!".into(),
            options: vec![],
            answered_at: None,
        })
        .await
        .unwrap_err()
        .contains("turn")
    );
    assert!(
        r.record_document(&DocumentInput {
            project: Some("nowhere".into()),
            ..text_doc("/work/demo/a.md", "a\n", "2026-10-27T01:00:00Z", None)
        })
        .await
        .unwrap_err()
        .contains("project")
    );
}

#[tokio::test]
async fn document_paths_and_kinds_are_checked() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    for bad in [
        "notes.md",
        "/work/demo/./a.md",
        "/work/../a.md",
        "/work//a.md",
        "/work/a\nb.md",
    ] {
        assert!(
            r.record_document(&text_doc(bad, "x\n", "2026-10-27T01:00:00Z", None))
                .await
                .is_err(),
            "{bad:?} accepted"
        );
    }
    assert!(
        r.record_document(&DocumentInput {
            kind: "artifact".into(),
            ..text_doc("/work/demo/a.md", "x\n", "2026-10-27T01:00:00Z", None)
        })
        .await
        .unwrap_err()
        .contains("artifact")
    );
    assert!(
        r.record_document(&DocumentInput {
            content: Content::Binary {
                hash: "c".repeat(64),
                size: 1
            },
            ..text_doc("/work/demo/b.bin", "", "2026-10-27T01:00:00Z", None)
        })
        .await
        .unwrap_err()
        .contains("artifact")
    );
    // Same body, different kind: recorded as a change, not ignored.
    r.record_document(&text_doc(
        "/work/demo/c.md",
        "c\n",
        "2026-10-27T01:00:00Z",
        None,
    ))
    .await
    .unwrap();
    let rekinded = r
        .record_document(&DocumentInput {
            kind: "etc".into(),
            ..text_doc("/work/demo/c.md", "c\n", "2026-10-27T01:01:00Z", None)
        })
        .await
        .unwrap();
    assert_eq!(rekinded.change.as_deref(), Some("modify"));
    assert_eq!(
        r.document("/work/demo/c.md").await.unwrap().unwrap().kind,
        "etc"
    );
}

#[tokio::test]
async fn deleted_documents_leave_the_index_and_korean_alone_matches() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    r.record_document(&text_doc(
        "/work/demo/wiki/db.md",
        "libSQL은 SQLite를 포크한 저장소다.\n",
        "2026-10-27T01:05:00Z",
        None,
    ))
    .await
    .unwrap();
    assert_eq!(
        r.search("포크", Scope::Documents, None, 10)
            .await
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        r.search("포크", Scope::Turns, None, 10)
            .await
            .unwrap()
            .len(),
        1
    );
    r.delete_document("/work/demo/wiki/db.md", "2026-10-27T01:06:00Z", None, None)
        .await
        .unwrap();
    assert!(
        r.search("포크", Scope::Documents, None, 10)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        r.search("포크", Scope::All, None, 10).await.unwrap().len(),
        1
    );
}

#[tokio::test]
async fn secrets_never_reach_the_database() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    let out = r
        .record_turn(&turn(
            "s1",
            3,
            "token ghp_abcdefghijklmnopqrstuvwxyz123456 써도 돼?",
            Some("API_KEY=topsecret999 는 넣지 마"),
        ))
        .await
        .unwrap();
    assert_eq!(out.redacted, 2);
    r.record_document(&text_doc(
        "/work/demo/notes.md",
        "DB_PASSWORD=hunter2hunter2\n",
        "2026-10-27T01:03:40Z",
        Some(("s1", 3)),
    ))
    .await
    .unwrap();
    r.record_question_reply(&QuestionReply {
        session_id: "s1".into(),
        turn_no: 3,
        question_id: "q-secret".into(),
        question: "어느 키?".into(),
        answer: "sk-live-abcdefghijklmnopqrstuvwxyz".into(),
        options: vec!["sk-live-abcdefghijklmnopqrstuvwxyz".into(), "없음".into()],
        answered_at: None,
    })
    .await
    .unwrap();

    let recent = r.recent(Some("demo"), None, 10).await.unwrap();
    let third = recent.iter().find(|t| t.turn.turn_no == 3).unwrap();
    let dump = format!("{third:?}");
    for secret in ["ghp_abcdef", "topsecret999", "sk-live-abcdef"] {
        assert!(!dump.contains(secret), "{secret} leaked: {dump}");
    }
    let doc = r.document("/work/demo/notes.md").await.unwrap().unwrap();
    assert!(!doc.body.unwrap().contains("hunter2"));
    let history = r.history("/work/demo/notes.md", 10).await.unwrap();
    assert!(!history[0].diff.as_deref().unwrap().contains("hunter2"));
}

#[tokio::test]
async fn documents_keep_a_unified_diff_history() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    let path = "/work/demo/STATUS.md";

    let created = r
        .record_document(&text_doc(
            path,
            "# 현황\n\n- 할 일 A\n",
            "2026-10-27T01:01:40Z",
            Some(("s1", 1)),
        ))
        .await
        .unwrap();
    assert_eq!(created.change.as_deref(), Some("create"));

    // Same body again: nothing recorded.
    let same = r
        .record_document(&text_doc(
            path,
            "# 현황\n\n- 할 일 A\n",
            "2026-10-27T01:01:50Z",
            Some(("s1", 1)),
        ))
        .await
        .unwrap();
    assert_eq!(same.change, None);
    assert_eq!(same.doc_id, created.doc_id);

    // Edited outside any session (Obsidian): session and turn stay empty.
    let edited = r
        .record_document(&text_doc(
            path,
            "# 현황\n\n- 할 일 A\n- 할 일 B\n",
            "2026-10-27T02:00:00Z",
            None,
        ))
        .await
        .unwrap();
    assert_eq!(edited.change.as_deref(), Some("modify"));

    let history = r.history(path, 10).await.unwrap();
    assert_eq!(history.len(), 2, "{history:?}");
    let latest = &history[0];
    assert_eq!(latest.change_kind, "modify");
    assert!(latest.session_id.is_none() && latest.turn_no.is_none());
    let diff = latest.diff.as_deref().unwrap();
    assert!(diff.contains("@@") && diff.contains("+- 할 일 B"), "{diff}");
    assert!(
        !diff.contains("-- 할 일 A"),
        "unchanged line must not be removed: {diff}"
    );
    assert_eq!(history[1].change_kind, "create");
    assert_eq!(history[1].session_id.as_deref(), Some("s1"));
    assert_eq!(history[1].turn_no, Some(1));

    let doc = r.document(path).await.unwrap().unwrap();
    assert_eq!(
        doc.body.as_deref(),
        Some("# 현황\n\n- 할 일 A\n- 할 일 B\n")
    );
    assert_eq!(doc.name, "STATUS.md");
    assert_eq!(doc.kind, "record");
    assert_eq!(doc.created_at, "2026-10-27T01:01:40Z");
    assert_eq!(doc.updated_at, "2026-10-27T02:00:00Z");
    assert_eq!(latest.body_hash_after.as_deref(), doc.body_hash.as_deref());

    // Rename keeps the history under the new path and records the old one.
    let renamed = r
        .rename_document(
            path,
            "/work/demo/STATE.md",
            "2026-10-27T03:00:00Z",
            Some("s1"),
            Some(2),
        )
        .await
        .unwrap();
    assert_eq!(renamed.change.as_deref(), Some("rename"));
    assert!(r.document(path).await.unwrap().is_none());
    let history = r.history("/work/demo/STATE.md", 10).await.unwrap();
    assert_eq!(history.len(), 3);
    assert!(history[0].diff.as_deref().unwrap().contains("STATUS.md"));

    // Delete keeps the row and its history; the last change says so.
    let deleted = r
        .delete_document("/work/demo/STATE.md", "2026-10-27T04:00:00Z", None, None)
        .await
        .unwrap();
    assert_eq!(deleted.change.as_deref(), Some("delete"));
    let history = r.history("/work/demo/STATE.md", 10).await.unwrap();
    assert_eq!(history[0].change_kind, "delete");
    assert_eq!(history.len(), 4);
}

#[tokio::test]
async fn artifacts_are_tracked_by_hash_and_size_only() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    let input = DocumentInput {
        path: "/work/demo/deck.pptx".into(),
        kind: "artifact".into(),
        content: Content::Binary {
            hash: "a".repeat(64),
            size: 48_213,
        },
        at: "2026-10-27T01:01:45Z".into(),
        session_id: Some("s1".into()),
        turn_no: Some(1),
        project: Some("demo".into()),
    };
    let created = r.record_document(&input).await.unwrap();
    assert_eq!(created.change.as_deref(), Some("create"));
    let doc = r.document("/work/demo/deck.pptx").await.unwrap().unwrap();
    assert_eq!(doc.body, None);
    assert_eq!(doc.body_hash.as_deref(), Some("a".repeat(64).as_str()));
    assert_eq!(doc.size, Some(48_213));

    let same = r.record_document(&input).await.unwrap();
    assert_eq!(same.change, None);

    let changed = r
        .record_document(&DocumentInput {
            content: Content::Binary {
                hash: "b".repeat(64),
                size: 50_000,
            },
            at: "2026-10-27T01:02:45Z".into(),
            turn_no: Some(2),
            ..input.clone()
        })
        .await
        .unwrap();
    assert_eq!(changed.change.as_deref(), Some("modify"));
    let history = r.history("/work/demo/deck.pptx", 10).await.unwrap();
    assert_eq!(history.len(), 2);
    assert_eq!(history[0].diff, None);
    assert_eq!(
        history[0].body_hash_after.as_deref(),
        Some("b".repeat(64).as_str())
    );
    assert_eq!(history[0].turn_no, Some(2));
}

#[tokio::test]
async fn question_replies_and_models_attach_to_their_turn() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    r.record_turn_model(&TurnModel {
        session_id: "s1".into(),
        turn_no: 1,
        model: "claude-opus-5-5".into(),
        effort: Some("medium".into()),
    })
    .await
    .unwrap();
    for (id, q, a) in [
        ("q1", "설치할까?", "brew로 설치"),
        ("q2", "범위는?", "전역 규칙까지"),
    ] {
        r.record_question_reply(&QuestionReply {
            session_id: "s1".into(),
            turn_no: 1,
            question_id: id.into(),
            question: q.into(),
            answer: a.into(),
            options: vec![a.into(), "다른 것".into()],
            answered_at: Some("2026-10-27T01:01:20Z".into()),
        })
        .await
        .unwrap();
    }
    // Resending the same reply is a no-op.
    let again = r
        .record_question_reply(&QuestionReply {
            session_id: "s1".into(),
            turn_no: 1,
            question_id: "q1".into(),
            question: "설치할까?".into(),
            answer: "brew로 설치".into(),
            options: vec!["brew로 설치".into(), "다른 것".into()],
            answered_at: Some("2026-10-27T01:01:20Z".into()),
        })
        .await
        .unwrap();
    assert!(!again.inserted);
    r.record_document(&text_doc(
        "/work/demo/wiki/a.md",
        "a\n",
        "2026-10-27T01:01:40Z",
        Some(("s1", 1)),
    ))
    .await
    .unwrap();

    let recent = r.recent(None, Some("s1"), 10).await.unwrap();
    let first = recent.iter().find(|t| t.turn.turn_no == 1).unwrap();
    assert_eq!(first.models.len(), 1);
    assert_eq!(first.models[0].effort.as_deref(), Some("medium"));
    let mut ids: Vec<&str> = first
        .replies
        .iter()
        .map(|q| q.question_id.as_str())
        .collect();
    ids.sort();
    assert_eq!(ids, ["q1", "q2"]);
    assert_eq!(
        first
            .replies
            .iter()
            .find(|q| q.question_id == "q2")
            .unwrap()
            .options,
        vec!["전역 규칙까지", "다른 것"]
    );
    assert_eq!(first.changed_documents, vec!["/work/demo/wiki/a.md"]);
}

#[tokio::test]
async fn search_is_korean_aware_and_scoped() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    r.upsert_project("other", "/work/other").await.unwrap();
    r.upsert_session(&session("s2", "other")).await.unwrap();
    r.record_turn(&turn("s2", 1, "다른 프로젝트에서 libSQL 질문", Some("답")))
        .await
        .unwrap();
    r.record_document(&text_doc(
        "/work/demo/wiki/db.md",
        "# libSQL\n\nlibSQL은 SQLite를 포크한 저장소다.\n",
        "2026-10-27T01:05:00Z",
        None,
    ))
    .await
    .unwrap();
    r.record_question_reply(&QuestionReply {
        session_id: "s1".into(),
        turn_no: 1,
        question_id: "q-db".into(),
        question: "어느 DB로 갈까?".into(),
        answer: "libSQL 유지".into(),
        options: vec!["libSQL 유지".into(), "SQLite 직접".into()],
        answered_at: None,
    })
    .await
    .unwrap();

    // A Korean particle on a Latin word still matches.
    let all = r.search("libSQL 포크", Scope::All, None, 10).await.unwrap();
    let kinds: Vec<&str> = all.iter().map(|h| h.kind.as_str()).collect();
    assert!(
        kinds.contains(&"turn") && kinds.contains(&"document") && kinds.contains(&"reply"),
        "{all:?}"
    );

    let docs = r
        .search("libSQL", Scope::Documents, None, 10)
        .await
        .unwrap();
    assert!(docs.iter().all(|h| h.kind == "document"));
    assert_eq!(docs[0].path.as_deref(), Some("/work/demo/wiki/db.md"));

    let demo_turns = r
        .search("libSQL", Scope::Turns, Some("demo"), 10)
        .await
        .unwrap();
    assert!(
        demo_turns
            .iter()
            .all(|h| h.session_id.as_deref() == Some("s1")),
        "{demo_turns:?}"
    );
    assert!(!demo_turns.is_empty());

    assert!(
        r.search("zzqqxx", Scope::All, None, 10)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn recent_orders_by_time_and_filters_by_project_or_session() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    r.upsert_session(&session("s2", "demo")).await.unwrap();
    r.record_turn(&turn("s2", 1, "나중 세션", Some("답")))
        .await
        .unwrap();
    r.record_turn(&turn("s1", 2, "첫 세션 두 번째", None))
        .await
        .unwrap();
    r.upsert_project("other", "/work/other").await.unwrap();
    r.upsert_session(&session("s9", "other")).await.unwrap();
    r.record_turn(&turn("s9", 1, "다른 프로젝트", Some("답")))
        .await
        .unwrap();

    let demo = r.recent(Some("demo"), None, 10).await.unwrap();
    let keys: Vec<(String, i64)> = demo
        .iter()
        .map(|t| (t.turn.session_id.clone(), t.turn.turn_no))
        .collect();
    // Newest first: s1#2 (01:02) before s2#1 and s1#1 (both 01:01); same time keeps session order stable.
    assert_eq!(keys[0], ("s1".to_owned(), 2));
    assert_eq!(keys.len(), 3);
    assert!(keys.iter().all(|(s, _)| s != "s9"));

    let only_s2 = r.recent(None, Some("s2"), 10).await.unwrap();
    assert_eq!(only_s2.len(), 1);
    assert_eq!(r.recent(Some("demo"), None, 2).await.unwrap().len(), 2);
}

#[tokio::test]
async fn sessions_need_a_known_project_and_global_documents_link_everywhere() {
    let dir = tempfile::tempdir().unwrap();
    let r = open(&dir).await;
    assert!(
        r.upsert_session(&session("s1", "missing"))
            .await
            .unwrap_err()
            .contains("project")
    );
    r.upsert_project("demo", "/work/demo").await.unwrap();
    r.upsert_project("other", "/work/other").await.unwrap();
    let global = "/Users/me/.claude/CLAUDE.md";
    for project in ["demo", "other"] {
        r.record_document(&DocumentInput {
            project: Some(project.into()),
            kind: "harness".into(),
            ..text_doc(global, "# rules\n", "2026-10-27T01:00:00Z", None)
        })
        .await
        .unwrap();
    }
    let doc = r.document(global).await.unwrap().unwrap();
    assert_eq!(doc.kind, "harness");
    // One document row, linked to both projects: it appears in both scoped searches.
    for project in ["demo", "other"] {
        let hits = r
            .search("rules", Scope::Documents, Some(project), 10)
            .await
            .unwrap();
        assert_eq!(hits.len(), 1, "{project}: {hits:?}");
        assert_eq!(hits[0].doc_id, Some(doc.doc_id));
    }
    assert_eq!(r.history(global, 10).await.unwrap().len(), 1);
}

async fn change_kinds(r: &Records, path: &str) -> Vec<String> {
    r.history(path, 10)
        .await
        .unwrap()
        .iter()
        .map(|c| c.change_kind.clone())
        .collect()
}

#[tokio::test]
async fn deleting_twice_and_replaying_a_rename_change_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    r.record_document(&text_doc(
        "/work/demo/a.md",
        "alpha\n",
        "2026-10-27T01:01:10Z",
        Some(("s1", 1)),
    ))
    .await
    .unwrap();
    let renamed = r
        .rename_document(
            "/work/demo/a.md",
            "/work/demo/b.md",
            "2026-10-27T01:02:00Z",
            Some("s1"),
            Some(1),
        )
        .await
        .unwrap();
    assert_eq!(renamed.change.as_deref(), Some("rename"));

    // The adapter resends the rename after losing the reply: the source is
    // gone and the target is there, so nothing happens.
    let again = r
        .rename_document(
            "/work/demo/a.md",
            "/work/demo/b.md",
            "2026-10-27T01:02:00Z",
            Some("s1"),
            Some(1),
        )
        .await
        .unwrap();
    assert_eq!((again.doc_id, again.change), (renamed.doc_id, None));
    // Source and target both unknown is still an error.
    assert!(
        r.rename_document(
            "/work/demo/x.md",
            "/work/demo/y.md",
            "2026-10-27T01:02:01Z",
            None,
            None
        )
        .await
        .is_err()
    );

    let deleted = r
        .delete_document(
            "/work/demo/b.md",
            "2026-10-27T01:03:00Z",
            Some("s1"),
            Some(1),
        )
        .await
        .unwrap();
    assert_eq!(deleted.change.as_deref(), Some("delete"));
    let again = r
        .delete_document(
            "/work/demo/b.md",
            "2026-10-27T01:03:00Z",
            Some("s1"),
            Some(1),
        )
        .await
        .unwrap();
    assert_eq!(again.change, None);
    assert_eq!(
        change_kinds(&r, "/work/demo/b.md").await,
        ["delete", "rename", "create"]
    );
}

#[tokio::test]
async fn renaming_onto_a_deleted_path_keeps_both_histories() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    let a = r
        .record_document(&text_doc(
            "/work/demo/a.md",
            "alpha\n",
            "2026-10-27T01:01:10Z",
            Some(("s1", 1)),
        ))
        .await
        .unwrap();
    let b = r
        .record_document(&text_doc(
            "/work/demo/b.md",
            "beta\n",
            "2026-10-27T01:01:20Z",
            Some(("s1", 1)),
        ))
        .await
        .unwrap();
    r.delete_document(
        "/work/demo/b.md",
        "2026-10-27T01:02:00Z",
        Some("s1"),
        Some(1),
    )
    .await
    .unwrap();

    // b.md's row keeps its history, so a.md's content moves into it and both
    // rows record the rename.
    let renamed = r
        .rename_document(
            "/work/demo/a.md",
            "/work/demo/b.md",
            "2026-10-27T01:03:00Z",
            Some("s1"),
            Some(1),
        )
        .await
        .unwrap();
    assert_eq!(
        (renamed.doc_id, renamed.change.as_deref()),
        (b.doc_id, Some("rename"))
    );
    let now_b = r.document("/work/demo/b.md").await.unwrap().unwrap();
    assert_eq!(
        (now_b.doc_id, now_b.body.as_deref()),
        (b.doc_id, Some("alpha\n"))
    );
    let old_a = r.document("/work/demo/a.md").await.unwrap().unwrap();
    assert_eq!((old_a.doc_id, old_a.body), (a.doc_id, None));
    assert_eq!(
        change_kinds(&r, "/work/demo/b.md").await,
        ["rename", "delete", "create"]
    );
    assert_eq!(
        change_kinds(&r, "/work/demo/a.md").await,
        ["rename", "create"]
    );
    let hits = r
        .search("alpha", Scope::Documents, Some("demo"), 10)
        .await
        .unwrap();
    assert_eq!(hits.len(), 1, "{hits:?}");
    assert_eq!(hits[0].path.as_deref(), Some("/work/demo/b.md"));

    // A live target is still a conflict.
    r.record_document(&text_doc(
        "/work/demo/c.md",
        "gamma\n",
        "2026-10-27T01:04:00Z",
        None,
    ))
    .await
    .unwrap();
    assert!(
        r.rename_document(
            "/work/demo/b.md",
            "/work/demo/c.md",
            "2026-10-27T01:05:00Z",
            None,
            None
        )
        .await
        .is_err()
    );
}

#[tokio::test]
async fn an_artifact_size_correction_is_recorded_as_a_change() {
    let dir = tempfile::tempdir().unwrap();
    let r = seeded(&dir).await;
    let artifact = |size: i64| DocumentInput {
        path: "/work/demo/out.pptx".into(),
        kind: "artifact".into(),
        content: Content::Binary {
            hash: "sha256:abc".into(),
            size,
        },
        at: "2026-10-27T01:01:10Z".into(),
        session_id: Some("s1".into()),
        turn_no: Some(1),
        project: Some("demo".into()),
    };
    assert_eq!(
        r.record_document(&artifact(0))
            .await
            .unwrap()
            .change
            .as_deref(),
        Some("create")
    );
    assert_eq!(
        r.record_document(&artifact(1234))
            .await
            .unwrap()
            .change
            .as_deref(),
        Some("modify")
    );
    assert_eq!(
        r.record_document(&artifact(1234)).await.unwrap().change,
        None
    );
    assert_eq!(
        r.document("/work/demo/out.pptx")
            .await
            .unwrap()
            .unwrap()
            .size,
        Some(1234)
    );
}
