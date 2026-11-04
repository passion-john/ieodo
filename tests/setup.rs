use ieodo::{http::router, mcp::Hub};
use serde_json::{Value, json};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::sync::Arc;

fn setup(home: &Path, root: &Path, mode: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_ieodo"))
        .args([
            "setup",
            "--project",
            "setup-test",
            "--harness",
            "both",
            "--project-dir",
        ])
        .arg(root)
        .arg(mode)
        .current_dir(root)
        .env("IEODO_HOME", home)
        .output()
        .unwrap()
}

fn hook(command: &str, payload: Value) -> Value {
    let mut child = Command::new("sh")
        .args(["-c", command])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(payload.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn bundled_setup_connects_installed_hooks_to_real_mcp_without_source_checkout() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("private home");
    let root = temp.path().join("project with spaces");
    std::fs::create_dir_all(home.join("continuity")).unwrap();
    std::fs::create_dir_all(&root).unwrap();
    let hub = Arc::new(Hub {
        records: ieodo::records::Records::open(&home.join("records.db"))
            .await
            .unwrap(),
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let shared = hub.clone();
    let server = tokio::spawn(async move { axum::serve(listener, router(hub)).await.unwrap() });
    let config = home.join("continuity/config.json");
    std::fs::write(
        &config,
        json!({"projects":[],"server_url":base}).to_string(),
    )
    .unwrap();

    let missing = setup(&home, &root, "--check");
    assert!(!missing.status.success());
    assert!(!root.join(".codex").exists());
    assert!(!home.join("continuity/state").exists());
    let installed = setup(&home, &root, "--apply");
    assert!(
        installed.status.success(),
        "{}",
        String::from_utf8_lossy(&installed.stderr)
    );
    let checked = setup(&home, &root, "--check");
    assert!(
        checked.status.success(),
        "{}",
        String::from_utf8_lossy(&checked.stderr)
    );
    let result: Value = serde_json::from_slice(&checked.stdout).unwrap();
    assert_eq!(result["configured"], true);
    assert_eq!(result["runtime_validation"], "not_checked");
    let installed_config = std::fs::read(&config).unwrap();
    assert!(setup(&home, &root, "--apply").status.success());
    assert_eq!(std::fs::read(&config).unwrap(), installed_config);

    let codex: Value =
        serde_json::from_slice(&std::fs::read(root.join(".codex/hooks.json")).unwrap()).unwrap();
    let claude: Value =
        serde_json::from_slice(&std::fs::read(root.join(".claude/settings.local.json")).unwrap())
            .unwrap();
    let command = codex["hooks"]["Stop"][0]["hooks"][0]["command"]
        .as_str()
        .unwrap();
    assert!(!command.contains(env!("CARGO_MANIFEST_DIR")));
    std::fs::write(root.join("STATUS.md"), "# 상태\n").unwrap();
    // Execute the exact installed commands with fixture lifecycle input, the
    // way the harness would. This verifies the integration, not the host's
    // hook trust/discovery or a model.
    let codex_hook = |event: &str, extra: Value| {
        let mut payload =
            json!({"hook_event_name": event, "cwd": root, "session_id": "codex-fixture"});
        payload
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        hook(command, payload)
    };
    let started = codex_hook("SessionStart", json!({"source": "startup"}));
    // Nothing is injected into a new session.
    assert!(started.get("hookSpecificOutput").is_none(), "{started}");
    assert!(started.get("systemMessage").is_none(), "{started}");
    let asked = codex_hook(
        "UserPromptSubmit",
        json!({"prompt": "Keep the blue button and the total 10600."}),
    );
    assert!(asked.get("systemMessage").is_none(), "{asked}");
    let answered = codex_hook("Stop", json!({"last_assistant_message": "Kept both."}));
    assert!(answered.get("systemMessage").is_none(), "{answered}");
    let start = claude["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        .as_str()
        .unwrap();
    assert!(!start.contains(env!("CARGO_MANIFEST_DIR")));

    let recent = shared
        .records
        .recent(Some("setup-test"), None, 10)
        .await
        .unwrap();
    assert_eq!(recent.len(), 1, "{recent:?}");
    assert_eq!(recent[0].turn.session_id, "codex:codex-fixture");
    assert_eq!(
        recent[0].turn.user_text,
        "Keep the blue button and the total 10600."
    );
    assert_eq!(recent[0].turn.assistant_text.as_deref(), Some("Kept both."));
    let status = shared
        .records
        .document(
            root.canonicalize()
                .unwrap()
                .join("STATUS.md")
                .to_str()
                .unwrap(),
        )
        .await
        .unwrap()
        .expect("STATUS.md snapshot");
    assert_eq!(status.kind, "record");
    server.abort();
}
