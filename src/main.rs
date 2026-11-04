use ieodo::mcp::Hub;
use ieodo::records::Records;
use ieodo::{http, records_path, stdio};
use std::sync::Arc;

const HELP: &str = "ieodo — a local record of AI conversations and document history (MCP)

USAGE:
  ieodo serve [--addr 127.0.0.1:7341]      run the shared server (all clients connect here)
  ieodo connect --client NAME [--url URL]   stdio bridge to the shared server
  ieodo stdio --client NAME                 stdio server that opens the database itself
  ieodo setup --project NAME --harness codex|claude|antigravity|both|all [--project-dir DIR] [--check|--apply]
                                                register this project and install capture hooks

ENV: IEODO_HOME (default ~/.ieodo), IEODO_ALLOW_REMOTE=1";

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

async fn open_hub() -> Result<Hub, String> {
    Ok(Hub {
        records: Records::open(&records_path()).await?,
    })
}

async fn run(args: Vec<String>) -> Result<(), String> {
    match args.first().map(String::as_str) {
        Some("serve") => {
            let addr = flag(&args, "--addr").unwrap_or_else(|| http::DEFAULT_ADDR.into());
            let addr = addr
                .parse()
                .map_err(|e| format!("bad --addr {addr}: {e}"))?;
            // Hooks run the adapter from a fixed path; a new server brings its version.
            ieodo::setup::refresh_runtime(&ieodo::data_dir())?;
            http::serve(Arc::new(open_hub().await?), addr).await
        }
        Some("connect") => {
            let client = flag(&args, "--client").ok_or("connect needs --client NAME")?;
            let url =
                flag(&args, "--url").unwrap_or_else(|| format!("http://{}", http::DEFAULT_ADDR));
            stdio::connect(&url, &client).await
        }
        Some("stdio") => {
            let client = flag(&args, "--client").ok_or("stdio needs --client NAME")?;
            stdio::direct(&open_hub().await?, &client).await
        }
        Some("setup") => ieodo::setup::run(&args[1..]),
        Some("-h" | "--help" | "help") | None => {
            println!("{HELP}");
            Ok(())
        }
        Some(other) => Err(format!("unknown command '{other}'\n\n{HELP}")),
    }
}

#[tokio::main]
async fn main() {
    if let Err(error) = run(std::env::args().skip(1).collect()).await {
        eprintln!("ieodo: {error}");
        std::process::exit(1);
    }
}
