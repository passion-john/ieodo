//! The `record` MCP tool (wiki/schema.md, "MCP 도구"): the capture adapter
//! sends one session's batch — the project, the session, user/assistant turn
//! pairs, the models behind them, document snapshots, renames, deletions and
//! question replies — and gets one outcome per item back. Only the adapter
//! calls it; items are applied in order and a bad item never stops the rest.

use ieodo::mcp::{self, Hub};
use ieodo::records::{Records, Scope};
use serde_json::{Value, json};

async fn hub(dir: &tempfile::TempDir) -> Hub {
    Hub {
        records: Records::open(&dir.path().join("records.db")).await.unwrap(),
    }
}

/// Calls `record`; a tool error comes back as `Err(text)`, success as the
/// parsed JSON reply.
async fn call(hub: &Hub, arguments: Value) -> Result<Value, String> {
    let reply = mcp::handle(
        hub,
        "claude-code",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "record", "arguments": arguments}}),
    )
    .await
    .unwrap();
    let text = reply["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_else(|| panic!("no text content: {reply}"))
        .to_owned();
    if reply["result"]["isError"] == true {
        Err(text)
    } else {
        Ok(serde_json::from_str(&text).unwrap_or_else(|e| panic!("{e}: {text}")))
    }
}

fn envelope(project: &str, session: &str) -> Value {
    json!({
        "project": {"name": project, "root_path": format!("/work/{project}")},
        "session": {
            "session_id": session,
            "harness": "claude-code",
            "started_at": "2026-10-27T01:00:00Z",
            "transcript_path": format!("/tmp/{session}.jsonl")
        }
    })
}

fn turn(no: i64, user: &str, assistant: Option<&str>) -> Value {
    let mut turn = json!({
        "turn_no": no,
        "user_text": user,
        "user_at": format!("2026-10-27T01:{no:02}:00Z")
    });
    if let Some(answer) = assistant {
        turn["assistant_text"] = answer.into();
        turn["assistant_at"] = format!("2026-10-27T01:{no:02}:30Z").into();
    }
    turn
}

fn snapshot(path: &str, body: &str, at: &str, turn_no: Option<i64>) -> Value {
    let mut doc = json!({"op": "snapshot", "path": path, "kind": "record", "body": body, "at": at});
    if let Some(no) = turn_no {
        doc["turn_no"] = no.into();
    }
    doc
}

fn items(errors: &Value) -> Vec<String> {
    let mut names: Vec<String> = errors
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            assert!(
                e["error"].as_str().is_some_and(|m| !m.is_empty()),
                "error entry without a message: {e}"
            );
            e["item"].as_str().unwrap().to_owned()
        })
        .collect();
    names.sort();
    names
}

#[tokio::test]
async fn record_registers_the_session_and_stores_one_batch() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([
        turn(
            1,
            "libSQL은 SQLite 포크야?",
            Some("맞아, libSQL은 SQLite의 포크야.")
        ),
        turn(2, "그럼 FTS5도 돼?", None)
    ]);
    args["models"] = json!([{"turn_no": 1, "model": "claude-opus-5-5", "effort": "high"}]);
    args["documents"] = json!([snapshot(
        "/work/demo/STATUS.md",
        "# 상태\n\n- 시작함\n",
        "2026-10-27T01:01:10Z",
        Some(1)
    )]);
    args["replies"] = json!([{
        "turn_no": 2, "question_id": "q1", "question": "어느 쪽으로 갈까?",
        "answer": "A", "options": ["A", "B"], "answered_at": "2026-10-27T01:02:40Z"
    }]);

    let out = call(&hub, args).await.unwrap();
    assert_eq!(out["session_id"], "s1");
    assert_eq!(
        out["turns"],
        json!({"inserted": 2, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(
        out["models"],
        json!({"inserted": 1, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(
        out["replies"],
        json!({"inserted": 1, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(out["documents"].as_array().unwrap().len(), 1, "{out}");
    assert_eq!(out["documents"][0]["path"], "/work/demo/STATUS.md");
    assert_eq!(out["documents"][0]["change"], "create");
    assert!(out["documents"][0]["doc_id"].is_i64(), "{out}");
    assert_eq!(out["redacted"], 0);
    assert_eq!(out["errors"], json!([]));

    let recent = hub.records.recent(Some("demo"), None, 10).await.unwrap();
    assert_eq!(recent.len(), 2);
    let first = recent.iter().find(|r| r.turn.turn_no == 1).unwrap();
    assert_eq!(first.turn.user_text, "libSQL은 SQLite 포크야?");
    assert_eq!(
        first.turn.assistant_text.as_deref(),
        Some("맞아, libSQL은 SQLite의 포크야.")
    );
    assert_eq!(
        first.turn.assistant_at.as_deref(),
        Some("2026-10-27T01:01:30Z")
    );
    assert_eq!(first.models.len(), 1);
    assert_eq!(first.models[0].model, "claude-opus-5-5");
    assert_eq!(first.models[0].effort.as_deref(), Some("high"));
    assert_eq!(
        first.changed_documents,
        vec!["/work/demo/STATUS.md".to_string()]
    );
    let second = recent.iter().find(|r| r.turn.turn_no == 2).unwrap();
    assert_eq!(second.turn.assistant_text, None);
    assert_eq!(second.replies.len(), 1);
    assert_eq!(second.replies[0].question_id, "q1");
    assert_eq!(second.replies[0].answer, "A");
    assert_eq!(
        second.replies[0].options,
        vec!["A".to_string(), "B".to_string()]
    );

    let doc = hub
        .records
        .document("/work/demo/STATUS.md")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(doc.kind, "record");
    assert_eq!(doc.body.as_deref(), Some("# 상태\n\n- 시작함\n"));
}

#[tokio::test]
async fn record_resend_is_a_noop_and_a_later_answer_fills_the_turn() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([
        turn(1, "첫 질문", Some("첫 답")),
        turn(2, "둘째 질문", None)
    ]);
    args["models"] = json!([{"turn_no": 1, "model": "claude-opus-5-5"}]);
    args["documents"] = json!([snapshot(
        "/work/demo/a.md",
        "a\n",
        "2026-10-27T01:01:10Z",
        Some(1)
    )]);
    args["replies"] = json!([{"turn_no": 1, "question_id": "q1", "question": "?", "answer": "!", "options": ["!", "?"]}]);
    call(&hub, args.clone()).await.unwrap();

    // The adapter resends after a crash or a timeout: nothing is duplicated
    // and nothing is refused.
    let again = call(&hub, args).await.unwrap();
    assert_eq!(
        again["turns"],
        json!({"inserted": 0, "unchanged": 2}),
        "{again}"
    );
    assert_eq!(
        again["models"],
        json!({"inserted": 0, "unchanged": 1}),
        "{again}"
    );
    assert_eq!(
        again["replies"],
        json!({"inserted": 0, "unchanged": 1}),
        "{again}"
    );
    assert_eq!(again["documents"][0]["change"], Value::Null, "{again}");
    assert_eq!(again["errors"], json!([]));
    assert_eq!(
        hub.records
            .recent(Some("demo"), None, 10)
            .await
            .unwrap()
            .len(),
        2
    );

    // The answer to turn 2 arrives in a later batch (the next hook run).
    let mut fill = envelope("demo", "s1");
    fill["turns"] = json!([turn(2, "둘째 질문", Some("둘째 답"))]);
    let filled = call(&hub, fill).await.unwrap();
    assert_eq!(filled["errors"], json!([]));
    let recent = hub
        .records
        .recent(Some("demo"), Some("s1"), 10)
        .await
        .unwrap();
    let second = recent.iter().find(|r| r.turn.turn_no == 2).unwrap();
    assert_eq!(second.turn.assistant_text.as_deref(), Some("둘째 답"));
    assert_eq!(
        second.turn.assistant_at.as_deref(),
        Some("2026-10-27T01:02:30Z")
    );
}

#[tokio::test]
async fn record_refuses_bad_envelopes_before_touching_anything() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;

    let mut unknown = envelope("demo", "s1");
    unknown["oops"] = json!(1);
    let err = call(&hub, unknown).await.unwrap_err();
    assert!(err.contains("invalid record arguments"), "{err}");

    let mut harness = envelope("demo", "s1");
    harness["session"]["harness"] = "gemini".into();
    let err = call(&hub, harness).await.unwrap_err();
    assert!(err.contains("harness"), "{err}");

    let mut antigravity = envelope("demo", "a1");
    antigravity["session"]["harness"] = "antigravity".into();
    call(&hub, antigravity).await.unwrap();

    let mut missing = envelope("demo", "s1");
    missing.as_object_mut().unwrap().remove("session");
    assert!(call(&hub, missing).await.is_err());

    // A type error names the field, never the offending value.
    let mut typed = envelope("demo", "s1");
    typed["project"] = json!("API_KEY=topsecret999");
    let err = call(&hub, typed).await.unwrap_err();
    assert!(err.contains("invalid record arguments"), "{err}");
    assert!(!err.contains("topsecret999"), "{err}");

    // A session is bound to one project for good: a batch that would move it
    // fails as a whole, so none of its turns land under the wrong project.
    call(&hub, envelope("demo", "s1")).await.unwrap();
    let mut moved = envelope("other", "s1");
    moved["turns"] = json!([turn(1, "옮기기", None)]);
    let err = call(&hub, moved).await.unwrap_err();
    assert!(err.contains("already belongs"), "{err}");
    assert!(
        hub.records
            .recent(Some("other"), None, 10)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        hub.records
            .recent(Some("demo"), None, 10)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn record_applies_every_good_item_and_names_each_bad_one() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([
        turn(1, "첫 질문", Some("첫 답")),
        turn(1, "같은 번호, 다른 말", Some("충돌")),
    ]);
    args["models"] = json!([
        {"turn_no": 9, "model": "gpt-6.1-sol"},
        {"turn_no": 1, "model": "claude-opus-5-5", "effort": "high"},
    ]);
    args["documents"] = json!([
        {"op": "snapshot", "path": "/work/demo/out.pptx", "kind": "artifact", "body": "글자", "at": "2026-10-27T01:01:10Z", "turn_no": 1},
        {"op": "snapshot", "path": "/work/demo/out.pptx", "kind": "artifact", "hash": "sha256:abc", "size": 1234, "at": "2026-10-27T01:01:11Z", "turn_no": 1},
        {"op": "delete", "path": "/work/demo/missing.md", "at": "2026-10-27T01:01:12Z", "turn_no": 1},
        {"op": "snapshot", "path": "/work/demo/../demo/odd.md", "kind": "record", "body": "x\n", "at": "2026-10-27T01:01:13Z"},
    ]);
    args["replies"] = json!([{"turn_no": 9, "question_id": "q9", "question": "?", "answer": "!"}]);

    let out = call(&hub, args).await.unwrap();
    assert_eq!(
        out["turns"],
        json!({"inserted": 1, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(
        out["models"],
        json!({"inserted": 1, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(
        out["replies"],
        json!({"inserted": 0, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(out["documents"].as_array().unwrap().len(), 1, "{out}");
    assert_eq!(out["documents"][0]["path"], "/work/demo/out.pptx");
    assert_eq!(out["documents"][0]["change"], "create");
    assert_eq!(
        items(&out["errors"]),
        vec![
            "documents[0]",
            "documents[2]",
            "documents[3]",
            "models[0]",
            "replies[0]",
            "turns[1]"
        ],
        "{out}"
    );
    // Each error names the item so the adapter can quarantine just that one.
    let errors = out["errors"].as_array().unwrap();
    assert!(
        errors
            .iter()
            .any(|e| e["item"] == "turns[1]" && e["turn_no"] == 1),
        "{out}"
    );
    assert!(
        errors
            .iter()
            .any(|e| e["item"] == "documents[2]" && e["path"] == "/work/demo/missing.md"),
        "{out}"
    );
    assert!(
        errors
            .iter()
            .any(|e| e["item"] == "replies[0]" && e["question_id"] == "q9"),
        "{out}"
    );

    let recent = hub.records.recent(Some("demo"), None, 10).await.unwrap();
    assert_eq!(recent.len(), 1);
    assert_eq!(recent[0].turn.user_text, "첫 질문");
    assert_eq!(recent[0].models[0].model, "claude-opus-5-5");
    assert_eq!(
        recent[0].changed_documents,
        vec!["/work/demo/out.pptx".to_string()]
    );
}

#[tokio::test]
async fn record_redacts_secrets_in_every_item_and_never_echoes_them() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([turn(
        1,
        "token ghp_abcdefghijklmnopqrstuvwxyz123456 써도 돼?",
        Some("API_KEY=topsecret999 는 넣지 마")
    )]);
    args["documents"] = json!([snapshot(
        "/work/demo/notes.md",
        "키는 sk-live-abcdefghijklmnopqrstuvwxyz 로 둠\n",
        "2026-10-27T01:01:10Z",
        Some(1)
    )]);
    args["replies"] = json!([{
        "turn_no": 1, "question_id": "q", "question": "어느 키?",
        "answer": "sk-live-abcdefghijklmnopqrstuvwxyz",
        "options": ["sk-live-abcdefghijklmnopqrstuvwxyz", "없음"]
    }]);

    let out = call(&hub, args).await.unwrap();
    assert_eq!(out["errors"], json!([]));
    assert!(out["redacted"].as_i64().unwrap() >= 4, "{out}");

    let recent = hub.records.recent(Some("demo"), None, 10).await.unwrap();
    let doc = hub
        .records
        .document("/work/demo/notes.md")
        .await
        .unwrap()
        .unwrap();
    let hits = hub
        .records
        .search("없음", Scope::All, Some("demo"), 10)
        .await
        .unwrap();
    let dump = format!("{out}\n{recent:?}\n{doc:?}\n{hits:?}");
    for secret in ["ghp_abcdef", "topsecret999", "sk-live-abcdef"] {
        assert!(!dump.contains(secret), "{secret} leaked: {dump}");
    }
    assert!(
        recent[0].turn.user_text.contains("[REDACTED_"),
        "{recent:?}"
    );
    assert!(
        doc.body.as_deref().unwrap().contains("[REDACTED_"),
        "{doc:?}"
    );
    assert!(
        recent[0].replies[0].answer.contains("[REDACTED_"),
        "{recent:?}"
    );
}

#[tokio::test]
async fn record_refuses_an_oversized_document_body_without_dropping_the_rest() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([turn(1, "큰 파일", None)]);
    args["documents"] = json!([
        snapshot(
            "/work/demo/huge.md",
            &"a".repeat(8 * 1024 * 1024 + 1),
            "2026-10-27T01:01:10Z",
            Some(1)
        ),
        snapshot(
            "/work/demo/small.md",
            "small\n",
            "2026-10-27T01:01:11Z",
            Some(1)
        ),
    ]);

    let out = call(&hub, args).await.unwrap();
    assert_eq!(items(&out["errors"]), vec!["documents[0]"], "{out}");
    assert!(
        out["errors"][0]["error"]
            .as_str()
            .unwrap()
            .contains("8 MiB"),
        "{out}"
    );
    assert_eq!(out["documents"].as_array().unwrap().len(), 1, "{out}");
    assert_eq!(out["documents"][0]["path"], "/work/demo/small.md");
    assert!(
        hub.records
            .document("/work/demo/huge.md")
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        hub.records
            .document("/work/demo/small.md")
            .await
            .unwrap()
            .is_some()
    );
}

#[tokio::test]
async fn record_keeps_document_history_across_rename_edit_and_delete() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut first = envelope("demo", "s1");
    first["turns"] = json!([turn(1, "문서 만들어", Some("만들었어"))]);
    first["documents"] = json!([snapshot(
        "/work/demo/a.md",
        "x\n",
        "2026-10-27T01:01:10Z",
        Some(1)
    )]);
    call(&hub, first).await.unwrap();

    let mut second = envelope("demo", "s1");
    second["documents"] = json!([
        {"op": "rename", "from": "/work/demo/a.md", "to": "/work/demo/b.md", "at": "2026-10-27T01:05:00Z", "turn_no": 1},
        // No turn_no: the adapter noticed a change made outside any session
        // (a person editing in Obsidian), so the change carries no session.
        snapshot("/work/demo/b.md", "x\ny\n", "2026-10-27T02:00:00Z", None),
        {"op": "delete", "path": "/work/demo/b.md", "at": "2026-10-27T02:10:00Z", "turn_no": 1},
    ]);
    let out = call(&hub, second).await.unwrap();
    assert_eq!(out["errors"], json!([]));
    let changes: Vec<&str> = out["documents"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["change"].as_str().unwrap())
        .collect();
    assert_eq!(changes, vec!["rename", "modify", "delete"], "{out}");
    assert!(
        out["documents"]
            .as_array()
            .unwrap()
            .iter()
            .all(|d| d["path"] == "/work/demo/b.md"),
        "{out}"
    );

    let history = hub.records.history("/work/demo/b.md", 10).await.unwrap();
    let kinds: Vec<&str> = history.iter().map(|c| c.change_kind.as_str()).collect();
    assert_eq!(kinds, vec!["delete", "modify", "rename", "create"]);
    let modify = &history[1];
    assert_eq!((modify.session_id.as_deref(), modify.turn_no), (None, None));
    assert!(modify.diff.as_deref().unwrap().contains("+y"), "{modify:?}");
    let rename = &history[2];
    assert_eq!(
        (rename.session_id.as_deref(), rename.turn_no),
        (Some("s1"), Some(1))
    );
    assert!(
        hub.records
            .document("/work/demo/a.md")
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        hub.records
            .document("/work/demo/b.md")
            .await
            .unwrap()
            .unwrap()
            .body,
        None
    );
}

#[tokio::test]
async fn record_is_listed_for_the_adapter_with_a_closed_schema() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let tools = mcp::handle(
        &hub,
        "claude-code",
        json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    )
    .await
    .unwrap();
    let record = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == "record")
        .unwrap_or_else(|| panic!("record missing: {tools}"))
        .clone();
    let schema = &record["inputSchema"];
    assert_eq!(schema["additionalProperties"], false);
    assert_eq!(schema["required"], json!(["project", "session"]));
    for list in ["turns", "models", "documents", "replies"] {
        assert_eq!(
            schema["properties"][list]["type"], "array",
            "{list}: {schema}"
        );
        assert_eq!(
            schema["properties"][list]["items"]["additionalProperties"], false,
            "{list}: {schema}"
        );
    }
    assert_eq!(
        schema["properties"]["session"]["properties"]["harness"]["enum"],
        json!(["claude-code", "codex", "antigravity"])
    );
    assert_eq!(
        schema["properties"]["documents"]["items"]["properties"]["op"]["enum"],
        json!(["snapshot", "rename", "delete"])
    );
    assert_eq!(
        schema["properties"]["documents"]["items"]["properties"]["kind"]["enum"],
        json!([
            "harness", "skill", "rule", "report", "record", "readme", "artifact", "etc"
        ])
    );
    let description = record["description"].as_str().unwrap();
    assert!(description.contains("capture adapter"), "{description}");
}

#[tokio::test]
async fn record_replays_of_renames_and_deletes_change_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut first = envelope("demo", "s1");
    first["turns"] = json!([turn(1, "문서 만들어", Some("만들었어"))]);
    first["documents"] = json!([snapshot(
        "/work/demo/a.md",
        "x\n",
        "2026-10-27T01:01:10Z",
        Some(1)
    )]);
    call(&hub, first).await.unwrap();

    let mut second = envelope("demo", "s1");
    second["documents"] = json!([
        {"op": "rename", "from": "/work/demo/a.md", "to": "/work/demo/b.md", "at": "2026-10-27T01:05:00Z", "turn_no": 1},
        {"op": "delete", "path": "/work/demo/b.md", "at": "2026-10-27T01:06:00Z", "turn_no": 1},
    ]);
    let out = call(&hub, second.clone()).await.unwrap();
    assert_eq!(out["errors"], json!([]), "{out}");

    // The reply was lost and the adapter sends the same batch again.
    let again = call(&hub, second).await.unwrap();
    assert_eq!(again["errors"], json!([]), "{again}");
    let changes: Vec<Value> = again["documents"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["change"].clone())
        .collect();
    assert_eq!(changes, vec![Value::Null, Value::Null], "{again}");
    let kinds: Vec<String> = hub
        .records
        .history("/work/demo/b.md", 10)
        .await
        .unwrap()
        .iter()
        .map(|c| c.change_kind.clone())
        .collect();
    assert_eq!(kinds, ["delete", "rename", "create"]);
}

#[tokio::test]
async fn record_isolates_a_malformed_item_and_never_shows_its_text() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([turn(1, "첫 질문", Some("첫 답"))]);
    args["models"] = json!([
        {"turn_no": "one", "model": "x"},
        {"turn_no": 1, "model": "claude-opus-5-5"},
    ]);
    args["replies"] = json!([{
        "turn_no": 1, "question_id": "q", "question": "?", "answer": "!",
        "options": "API_KEY=topsecret999"
    }]);

    let out = call(&hub, args).await.unwrap();
    assert_eq!(
        out["turns"],
        json!({"inserted": 1, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(
        out["models"],
        json!({"inserted": 1, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(
        items(&out["errors"]),
        vec!["models[0]", "replies[0]"],
        "{out}"
    );
    assert!(!out.to_string().contains("topsecret999"), "{out}");
}

#[tokio::test]
async fn record_echoes_paths_redacted_but_stores_them_as_given() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let secret_path = "/work/demo/ghp_abcdefghijklmnopqrstuvwxyz123456.md";
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([turn(1, "이상한 이름", None)]);
    args["documents"] = json!([
        snapshot(secret_path, "x\n", "2026-10-27T01:01:10Z", Some(1)),
        {"op": "delete", "path": "/work/demo/ghp_zyxwvutsrqponmlkjihgfedcba654321.md", "at": "2026-10-27T01:01:11Z", "turn_no": 1},
    ]);

    let out = call(&hub, args).await.unwrap();
    assert!(!out.to_string().contains("ghp_abcdef"), "{out}");
    assert!(!out.to_string().contains("ghp_zyxwvu"), "{out}");
    assert!(
        out["documents"][0]["path"]
            .as_str()
            .unwrap()
            .contains("[REDACTED_"),
        "{out}"
    );
    assert_eq!(items(&out["errors"]), vec!["documents[1]"], "{out}");
    assert!(
        out["errors"][0]["path"]
            .as_str()
            .unwrap()
            .contains("[REDACTED_"),
        "{out}"
    );
    // The path is the document's identity, so the store keeps it as given.
    assert!(hub.records.document(secret_path).await.unwrap().is_some());
}

#[tokio::test]
async fn record_adds_messages_beside_a_turn_append_only_and_redacted() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let mut args = envelope("demo", "s1");
    args["turns"] = json!([turn(1, "첫 지시", Some("최종 보고"))]);
    args["messages"] = json!([
        {"turn_no": 1, "seq": 1, "text": "부연 설명입니다", "sent_at": "2026-10-27T01:00:20Z"},
        {"turn_no": 1, "seq": 2, "text": "GITHUB_TOKEN=ghp_abcdefghijklmnop1234567890", "sent_at": "2026-10-27T01:00:30Z"},
        {"turn_no": 9, "seq": 1, "text": "대화 쌍 없음", "sent_at": "2026-10-27T01:00:40Z"}
    ]);
    let out = call(&hub, args.clone()).await.unwrap();
    assert_eq!(
        out["messages"],
        json!({"inserted": 2, "unchanged": 0}),
        "{out}"
    );
    assert_eq!(items(&out["errors"]), vec!["messages[2]"], "{out}");

    // A resend changes nothing; different text under the same number is refused.
    args["messages"] = json!([
        {"turn_no": 1, "seq": 1, "text": "부연 설명입니다", "sent_at": "2026-10-27T01:00:20Z"},
        {"turn_no": 1, "seq": 1, "text": "바꾼 말", "sent_at": "2026-10-27T01:00:20Z"}
    ]);
    let out = call(&hub, args).await.unwrap();
    assert_eq!(
        out["messages"],
        json!({"inserted": 0, "unchanged": 1}),
        "{out}"
    );
    assert_eq!(items(&out["errors"]), vec!["messages[1]"], "{out}");

    let recent = hub.records.recent(Some("demo"), None, 5).await.unwrap();
    assert_eq!(recent[0].turn.user_text, "첫 지시");
    assert_eq!(recent[0].messages.len(), 2);
    assert_eq!(recent[0].messages[0].sent_at, "2026-10-27T01:00:20Z");
    assert!(!recent[0].messages[1].text.contains("ghp_abcdefghijklmnop"));

    for scope in [Scope::All, Scope::Turns] {
        let hits = hub
            .records
            .search("부연", scope, Some("demo"), 5)
            .await
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(
            (hits[0].kind.as_str(), hits[0].turn_no),
            ("message", Some(1))
        );
        assert_eq!(hits[0].excerpt, "부연 설명입니다");
    }
    assert!(
        hub.records
            .search("부연", Scope::Documents, None, 5)
            .await
            .unwrap()
            .is_empty()
    );

    for sql in [
        "UPDATE turn_message SET text = 'x'",
        "DELETE FROM turn_message",
    ] {
        let error = hub.records.raw_exec(sql).await.unwrap_err();
        assert!(error.contains("append-only"), "{error}");
    }
}
