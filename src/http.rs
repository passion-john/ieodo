//! One long-running server that every client shares (MCP Streamable HTTP,
//! JSON responses). Clients connect to `/mcp/<client-name>`.

use crate::mcp::{self, Hub};
use axum::extract::{DefaultBodyLimit, Path, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};
use std::net::SocketAddr;
use std::sync::Arc;

pub const DEFAULT_ADDR: &str = "127.0.0.1:7341";

fn valid_client(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 40
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '-' | '_'))
}

/// Browsers send an Origin header; only local pages may reach the server
/// (guards against DNS-rebinding attacks from web pages).
pub(crate) fn origin_allowed(headers: &HeaderMap) -> bool {
    match headers.get(header::ORIGIN).map(|v| v.to_str()) {
        None => true,
        // An Origin that is not even text is not a local page.
        Some(Err(_)) => false,
        Some(Ok(origin)) => [
            "http://127.0.0.1",
            "http://localhost",
            "https://127.0.0.1",
            "https://localhost",
        ]
        .iter()
        .any(|allowed| origin == *allowed || origin.starts_with(&format!("{allowed}:"))),
    }
}

async fn dispatch(hub: Arc<Hub>, client: String, headers: HeaderMap, body: Value) -> Response {
    if !origin_allowed(&headers) {
        return (StatusCode::FORBIDDEN, "origin not allowed").into_response();
    }
    if !valid_client(&client) {
        return (
            StatusCode::BAD_REQUEST,
            "client name must be 1..40 chars of a-z, 0-9, '-', '_'",
        )
            .into_response();
    }
    let replies: Vec<Value> = match body {
        Value::Array(batch) => {
            let mut out = Vec::new();
            for message in batch {
                if let Some(reply) = mcp::handle(&hub, &client, message).await {
                    out.push(reply);
                }
            }
            out
        }
        message => mcp::handle(&hub, &client, message)
            .await
            .into_iter()
            .collect(),
    };
    match replies.len() {
        0 => StatusCode::ACCEPTED.into_response(),
        1 => Json(replies.into_iter().next().unwrap_or(Value::Null)).into_response(),
        _ => Json(Value::Array(replies)).into_response(),
    }
}

async fn post_named(
    State(hub): State<Arc<Hub>>,
    Path(client): Path<String>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    dispatch(hub, client, headers, body).await
}

async fn post_anonymous(
    State(hub): State<Arc<Hub>>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    dispatch(hub, "unknown".into(), headers, body).await
}

async fn health(State(hub): State<Arc<Hub>>) -> Response {
    match hub.records.counts().await {
        Ok(counts) => Json(json!({
            "server": "ieodo",
            "version": env!("CARGO_PKG_VERSION"),
            "database": hub.records.path().display().to_string(),
            "records": counts,
        }))
        .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

async fn no_stream() -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        "this server answers POST requests with JSON; no SSE stream",
    )
        .into_response()
}

pub fn router(hub: Arc<Hub>) -> Router {
    Router::new()
        .route("/mcp", post(post_anonymous).get(no_stream))
        .route("/mcp/{client}", post(post_named).get(no_stream))
        .route("/health", get(health))
        // A capture batch stays under 40 MB.
        .layer(DefaultBodyLimit::max(50 * 1024 * 1024))
        .with_state(hub)
}

pub async fn serve(hub: Arc<Hub>, addr: SocketAddr) -> Result<(), String> {
    if !addr.ip().is_loopback() && std::env::var("IEODO_ALLOW_REMOTE").as_deref() != Ok("1") {
        return Err(format!(
            "refusing to listen on {addr}: only loopback addresses are allowed"
        ));
    }
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| format!("bind {addr}: {e}"))?;
    eprintln!("ieodo: serving MCP on http://{addr}/mcp/<client>");
    axum::serve(listener, router(hub))
        .with_graceful_shutdown(async {
            if let Err(error) = tokio::signal::ctrl_c().await {
                eprintln!("ieodo: shutdown signal error: {error}");
            }
        })
        .await
        .map_err(|e| format!("server error: {e}"))
}
