//! One HTTP server for every client: the capture adapter records through one
//! connection, any AI reads through another, and the transport rules hold.

use ieodo::http::router;
use ieodo::mcp::Hub;
use ieodo::records::Records;
use serde_json::{Value, json};
use std::sync::Arc;

async fn call(
    http: &reqwest::Client,
    base: &str,
    client: &str,
    name: &str,
    arguments: Value,
) -> Value {
    let reply: Value = http
        .post(format!("{base}/mcp/{client}"))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(reply["result"]["isError"], false, "{reply}");
    serde_json::from_str(reply["result"]["content"][0]["text"].as_str().unwrap()).unwrap()
}

#[tokio::test]
async fn one_server_records_for_one_client_and_answers_another() {
    let dir = tempfile::tempdir().unwrap();
    let hub = Arc::new(Hub {
        records: Records::open(&dir.path().join("records.db")).await.unwrap(),
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, router(hub)).await.unwrap() });
    let http = reqwest::Client::new();

    // Many batches at once from the adapter, then a read from another client.
    let mut tasks = Vec::new();
    for n in 1..=10 {
        let (http, base) = (http.clone(), base.clone());
        tasks.push(tokio::spawn(async move {
            call(&http, &base, "codex", "record", json!({
                "project": {"name": "load", "root_path": "/work/load"},
                "session": {"session_id": format!("codex:s{n}"), "harness": "codex", "started_at": "2026-10-28T01:00:00Z"},
                "turns": [{"turn_no": 1, "user_text": format!("래그돌 임펄스 {n}번 조정"),
                           "user_at": format!("2026-10-28T01:{n:02}:00Z"),
                           "assistant_text": "조정했어", "assistant_at": format!("2026-10-28T01:{n:02}:30Z")}],
                "models": [{"turn_no": 1, "model": "gpt-6.1-sol", "effort": "high"}]
            })).await
        }));
    }
    for task in tasks {
        assert_eq!(task.await.unwrap()["errors"], json!([]));
    }
    let found = call(
        &http,
        &base,
        "claude-code",
        "search",
        json!({"query": "래그돌", "project": "load"}),
    )
    .await;
    assert_eq!(found["hits"].as_array().unwrap().len(), 10, "{found}");
    assert_eq!(found["hits"][0]["models"][0]["model"], "gpt-6.1-sol");
    let recent = call(
        &http,
        &base,
        "claude-code",
        "recent",
        json!({"project": "load", "limit": 3}),
    )
    .await;
    let turns = recent["turns"].as_array().unwrap();
    assert_eq!(turns.len(), 3);
    assert_eq!(turns[0]["user_text"], "래그돌 임펄스 10번 조정");

    // Transport rules.
    let notification = http
        .post(format!("{base}/mcp/agy"))
        .json(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        .send()
        .await
        .unwrap();
    assert_eq!(notification.status(), 202);
    let evil = http
        .post(format!("{base}/mcp/agy"))
        .header("Origin", "https://evil.example")
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        .send()
        .await
        .unwrap();
    assert_eq!(evil.status(), 403);
    let bad_name = http
        .post(format!("{base}/mcp/Bad%20Name"))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        .send()
        .await
        .unwrap();
    assert_eq!(bad_name.status(), 400);
    assert_eq!(
        http.get(format!("{base}/mcp/agy"))
            .send()
            .await
            .unwrap()
            .status(),
        405
    );
    // The browser UI is gone.
    assert_eq!(
        http.get(format!("{base}/ui"))
            .send()
            .await
            .unwrap()
            .status(),
        404
    );
    let health: Value = http
        .get(format!("{base}/health"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(health["records"]["turns"], 10, "{health}");
    assert_eq!(health["records"]["sessions"], 10, "{health}");
}
