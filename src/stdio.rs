//! stdio transports. `connect` forwards each message to the shared HTTP
//! server, so clients that only speak stdio still use the one server.
//! `direct` opens the database itself (tests, or when no server runs).

use crate::mcp::{self, Hub};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

async fn write_line(out: &mut tokio::io::Stdout, value: &Value) -> Result<(), String> {
    let mut line = serde_json::to_string(value).map_err(|e| e.to_string())?;
    line.push('\n');
    out.write_all(line.as_bytes())
        .await
        .map_err(|e| e.to_string())?;
    out.flush().await.map_err(|e| e.to_string())
}

fn parse_error() -> Value {
    json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "parse error"}})
}

pub async fn direct(hub: &Hub, client: &str) -> Result<(), String> {
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let mut out = tokio::io::stdout();
    while let Some(line) = lines.next_line().await.map_err(|e| e.to_string())? {
        if line.trim().is_empty() {
            continue;
        }
        let reply = match serde_json::from_str::<Value>(&line) {
            Ok(message) => mcp::handle(hub, client, message).await,
            Err(_) => Some(parse_error()),
        };
        if let Some(reply) = reply {
            write_line(&mut out, &reply).await?;
        }
    }
    Ok(())
}

pub async fn connect(base_url: &str, client: &str) -> Result<(), String> {
    let url = format!("{}/mcp/{client}", base_url.trim_end_matches('/'));
    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let mut out = tokio::io::stdout();
    while let Some(line) = lines.next_line().await.map_err(|e| e.to_string())? {
        if line.trim().is_empty() {
            continue;
        }
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            write_line(&mut out, &parse_error()).await?;
            continue;
        };
        let id = message.get("id").cloned();
        let reply = match http.post(&url).json(&message).send().await {
            Ok(response) if response.status() == reqwest::StatusCode::ACCEPTED => None,
            Ok(response) if response.status().is_success() => match response.json::<Value>().await {
                Ok(value) => Some(value),
                Err(e) => id.map(|id| json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32603, "message": format!("bad reply from ieodo server: {e}")}})),
            },
            Ok(response) => id.map(|id| json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32603, "message": format!("ieodo server returned HTTP {}", response.status())}})),
            Err(e) => id.map(|id| json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32603, "message": format!("ieodo server is not reachable at {url} ({e}); start it with `ieodo serve`")}})),
        };
        if let Some(reply) = reply {
            write_line(&mut out, &reply).await?;
        }
    }
    Ok(())
}
