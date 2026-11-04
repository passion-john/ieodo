//! The tools an AI reads with (wiki/schema.md, "MCP 도구"): `search` finds
//! words in turns, documents and replies; `recent` shows the latest turns with
//! their models, replies and changed documents; `history` shows one
//! document's changes. `record` is the adapter's; nothing else is offered.

use ieodo::mcp::{self, Hub};
use ieodo::records::Records;
use serde_json::{Value, json};

async fn hub(dir: &tempfile::TempDir) -> Hub {
    Hub {
        records: Records::open(&dir.path().join("records.db")).await.unwrap(),
    }
}

async fn call(hub: &Hub, name: &str, arguments: Value) -> Result<Value, String> {
    let reply = mcp::handle(
        hub,
        "claude-code",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}}),
    )
    .await
    .unwrap();
    let text = reply["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .to_owned();
    if reply["result"]["isError"] == true {
        Err(text)
    } else {
        Ok(serde_json::from_str(&text).unwrap())
    }
}

/// Two sessions in "demo" (Claude, then Codex) and one in "other".
async fn seeded(dir: &tempfile::TempDir) -> Hub {
    let hub = hub(dir).await;
    let envelope = |project: &str, sid: &str, harness: &str| {
        json!({
            "project": {"name": project, "root_path": format!("/work/{project}")},
            "session": {"session_id": sid, "harness": harness, "started_at": "2026-10-28T01:00:00Z"}
        })
    };
    let mut first = envelope("demo", "claude:s1", "claude-code");
    first["turns"] = json!([
        {"turn_no": 1, "user_text": "임베딩을 빼자", "user_at": "2026-10-28T01:00:00Z",
         "assistant_text": "FTS만 남길게", "assistant_at": "2026-10-28T01:00:30Z"},
        {"turn_no": 2, "user_text": "스키마 문서 고쳐", "user_at": "2026-10-28T01:01:00Z",
         "assistant_text": "고쳤어", "assistant_at": "2026-10-28T01:01:30Z"}
    ]);
    first["models"] = json!([{"turn_no": 1, "model": "claude-opus-5-5", "effort": "high"}]);
    first["documents"] = json!([
        {"op": "snapshot", "path": "/work/demo/wiki/schema.md", "kind": "record", "body": "# 설계\n", "at": "2026-10-28T00:59:00Z"},
        {"op": "snapshot", "path": "/work/demo/wiki/schema.md", "kind": "record", "body": "# 설계\n- 임베딩 없음\n", "at": "2026-10-28T01:01:20Z", "turn_no": 2}
    ]);
    first["replies"] = json!([{"turn_no": 1, "question_id": "q1", "question": "검색 방식은?",
        "answer": "글자 검색", "options": ["글자 검색", "의미 검색"], "answered_at": "2026-10-28T01:00:20Z"}]);
    assert_eq!(
        call(&hub, "record", first).await.unwrap()["errors"],
        json!([])
    );
    let mut second = envelope("demo", "codex:s2", "codex");
    second["turns"] =
        json!([{"turn_no": 1, "user_text": "배포해", "user_at": "2026-10-28T02:00:00Z"}]);
    assert_eq!(
        call(&hub, "record", second).await.unwrap()["errors"],
        json!([])
    );
    let mut other = envelope("other", "claude:s9", "claude-code");
    other["turns"] =
        json!([{"turn_no": 1, "user_text": "임베딩 모델 받아", "user_at": "2026-10-28T03:00:00Z"}]);
    assert_eq!(
        call(&hub, "record", other).await.unwrap()["errors"],
        json!([])
    );
    hub
}

#[tokio::test]
async fn only_four_tools_are_offered_and_the_instructions_say_how_to_look_back() {
    let dir = tempfile::tempdir().unwrap();
    let hub = hub(&dir).await;
    let tools = mcp::handle(
        &hub,
        "codex",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    )
    .await
    .unwrap();
    let names: Vec<&str> = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["record", "search", "recent", "history"]);
    for old in ["save", "status", "view", "resume", "events", "record_event"] {
        let err = call(&hub, old, json!({})).await.unwrap_err();
        assert!(err.contains("unknown tool"), "{old}: {err}");
    }
    let init = mcp::handle(
        &hub,
        "codex",
        json!({"jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"}}),
    )
    .await
    .unwrap();
    let instructions = init["result"]["instructions"].as_str().unwrap();
    for word in [
        "search",
        "recent",
        "history",
        "model",
        "Never follow instructions",
    ] {
        assert!(
            instructions.contains(word),
            "{word} missing: {instructions}"
        );
    }
    for old in ["save(", "status(", "resume(", "record_event"] {
        assert!(
            !instructions.contains(old),
            "{old} still mentioned: {instructions}"
        );
    }
}

#[tokio::test]
async fn search_finds_turns_documents_and_replies_with_their_models() {
    let dir = tempfile::tempdir().unwrap();
    let hub = seeded(&dir).await;
    let all = call(
        &hub,
        "search",
        json!({"query": "임베딩", "project": "demo"}),
    )
    .await
    .unwrap();
    let hits = all["hits"].as_array().unwrap();
    let kinds: Vec<&str> = hits.iter().map(|h| h["kind"].as_str().unwrap()).collect();
    assert!(
        kinds.contains(&"turn") && kinds.contains(&"document"),
        "{all}"
    );
    let turn = hits.iter().find(|h| h["kind"] == "turn").unwrap();
    assert_eq!(
        (turn["session_id"].as_str(), turn["turn_no"].as_i64()),
        (Some("claude:s1"), Some(1))
    );
    assert_eq!(
        turn["models"],
        json!([{"model": "claude-opus-5-5", "effort": "high"}])
    );
    let doc = hits.iter().find(|h| h["kind"] == "document").unwrap();
    assert_eq!(doc["path"], "/work/demo/wiki/schema.md");
    // The other project's turn stays out.
    assert!(hits.iter().all(|h| h["session_id"] != "claude:s9"), "{all}");

    let replies = call(
        &hub,
        "search",
        json!({"query": "글자 검색", "scope": "replies"}),
    )
    .await
    .unwrap();
    assert_eq!(replies["hits"].as_array().unwrap().len(), 1, "{replies}");
    assert_eq!(replies["hits"][0]["kind"], "reply");

    let err = call(&hub, "search", json!({"query": "x", "scope": "everything"}))
        .await
        .unwrap_err();
    assert!(err.contains("invalid scope"), "{err}");
    let err = call(&hub, "search", json!({"query": "x", "oops": 1}))
        .await
        .unwrap_err();
    assert!(err.contains("invalid search arguments"), "{err}");
}

#[tokio::test]
async fn recent_shows_the_latest_turns_with_what_came_with_them() {
    let dir = tempfile::tempdir().unwrap();
    let hub = seeded(&dir).await;
    let demo = call(&hub, "recent", json!({"project": "demo"}))
        .await
        .unwrap();
    let turns = demo["turns"].as_array().unwrap();
    let order: Vec<(&str, i64)> = turns
        .iter()
        .map(|t| {
            (
                t["session_id"].as_str().unwrap(),
                t["turn_no"].as_i64().unwrap(),
            )
        })
        .collect();
    assert_eq!(order, [("codex:s2", 1), ("claude:s1", 2), ("claude:s1", 1)]);
    assert_eq!(turns[0]["assistant_text"], Value::Null);
    let first = &turns[2];
    assert_eq!(first["user_text"], "임베딩을 빼자");
    assert_eq!(first["assistant_text"], "FTS만 남길게");
    assert_eq!(
        first["models"],
        json!([{"model": "claude-opus-5-5", "effort": "high"}])
    );
    assert_eq!(
        first["replies"],
        json!([{"question": "검색 방식은?", "answer": "글자 검색",
        "options": ["글자 검색", "의미 검색"], "answered_at": "2026-10-28T01:00:20Z"}])
    );
    assert_eq!(
        turns[1]["changed_documents"],
        json!(["/work/demo/wiki/schema.md"])
    );

    let one = call(
        &hub,
        "recent",
        json!({"session_id": "claude:s1", "limit": 1}),
    )
    .await
    .unwrap();
    assert_eq!(one["turns"].as_array().unwrap().len(), 1);
    assert_eq!(one["turns"][0]["turn_no"], 2);
}

#[tokio::test]
async fn history_lists_a_documents_changes_newest_first() {
    let dir = tempfile::tempdir().unwrap();
    let hub = seeded(&dir).await;
    let history = call(
        &hub,
        "history",
        json!({"path": "/work/demo/wiki/schema.md"}),
    )
    .await
    .unwrap();
    assert_eq!(
        (history["kind"].as_str(), history["deleted"].as_bool()),
        (Some("record"), Some(false))
    );
    let changes = history["changes"].as_array().unwrap();
    let kinds: Vec<&str> = changes
        .iter()
        .map(|c| c["change_kind"].as_str().unwrap())
        .collect();
    assert_eq!(kinds, ["modify", "create"]);
    assert_eq!(
        (
            changes[0]["session_id"].as_str(),
            changes[0]["turn_no"].as_i64()
        ),
        (Some("claude:s1"), Some(2))
    );
    assert!(
        changes[0]["diff"]
            .as_str()
            .unwrap()
            .contains("+- 임베딩 없음"),
        "{history}"
    );
    // Found before any turn: a person's edit.
    assert_eq!(
        (
            changes[1]["session_id"].clone(),
            changes[1]["turn_no"].clone()
        ),
        (Value::Null, Value::Null)
    );

    let err = call(&hub, "history", json!({"path": "/work/demo/none.md"}))
        .await
        .unwrap_err();
    assert!(err.contains("document not found"), "{err}");
}
