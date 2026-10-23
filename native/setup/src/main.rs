//! Local development transactions only. There is deliberately no production
//! root selection, network/update service, signature bypass, GC or data deletion.
mod payload;
mod state;

use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    env,
    io::{self, BufRead, Read},
    path::PathBuf,
    process::ExitCode,
};

type Result<T> = std::result::Result<T, &'static str>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    schema: String,
    id: String,
    op: String,
    manifest: Option<PathBuf>,
    payload: Option<PathBuf>,
    manifest_sha256: Option<String>,
    plan_id: Option<String>,
    release_id: Option<String>,
}

fn execute(root: PathBuf, request: &Request) -> Result<Value> {
    if request.schema != "vep.setup.v1" || request.id.is_empty() || request.id.len() > 128 {
        return Err("PROTOCOL_UNSUPPORTED");
    }
    if request.op == "capabilities" {
        return Ok(
            json!({"development_only":true,"max_requests":1,"network":false,
            "operations":["init-sandbox","plan","apply","status","rollback","repair"],
            "production_install":false,"bootstrap_state":"vep.bootstrap-state.v1"}),
        );
    }
    if request.op == "init-sandbox" {
        state::Sandbox::initialize(&root)?;
    }
    let sandbox = state::Sandbox::open(root)?;
    match request.op.as_str() {
        "init-sandbox" | "status" => sandbox.status(),
        "plan" | "apply" => {
            let input = payload::Input::load(
                request.manifest.as_deref().ok_or("INVALID_REQUEST")?,
                request.payload.as_deref().ok_or("INVALID_REQUEST")?,
                request
                    .manifest_sha256
                    .as_deref()
                    .ok_or("INVALID_REQUEST")?,
            )?;
            if request.op == "plan" {
                sandbox.plan(&input)
            } else {
                sandbox.apply(&input, request.plan_id.as_deref().ok_or("INVALID_REQUEST")?)
            }
        }
        "rollback" => sandbox.rollback(
            request.release_id.as_deref().ok_or("INVALID_REQUEST")?,
            request.plan_id.as_deref().ok_or("INVALID_REQUEST")?,
        ),
        "repair" => sandbox.repair(),
        _ => Err("OPERATION_UNSUPPORTED"),
    }
}

fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let mut request_id = None;
    let outcome = (|| -> Result<Value> {
        if args.len() < 3 || args[0] != "--sandbox-root" {
            return Err("DEVELOPMENT_SANDBOX_REQUIRED");
        }
        let root = PathBuf::from(&args[1]);
        if args[2] == "run" {
            // Worker stdio belongs to the public VEP CLI, not to setup JSONL.
            let mut rest = &args[3..];
            let mut release = None;
            if rest.len() >= 2 && rest[0] == "--release" {
                release = rest[1].to_str();
                rest = &rest[2..];
            }
            if rest.first().is_none_or(|v| v != "--") {
                return Err("INVALID_REQUEST");
            }
            let code = state::Sandbox::open(root)?.launch(release, &rest[1..])?;
            std::process::exit(code);
        }
        if args.len() != 3 || args[2] != "--protocol=jsonl" {
            return Err("PROTOCOL_UNSUPPORTED");
        }
        let mut bytes = Vec::new();
        io::stdin()
            .lock()
            .take(65537)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "INVALID_REQUEST")?;
        if bytes.len() > 65536 {
            return Err("REQUEST_TOO_LARGE");
        }
        let request: Request = serde_json::from_slice(&bytes).map_err(|_| "INVALID_REQUEST")?;
        request_id = Some(request.id.clone());
        let value = execute(root, &request)?;
        Ok(
            json!({"schema":"vep.setup.v1","id":request.id,"seq":1,"event":"result","status":"ok","value":value}),
        )
    })();
    match outcome {
        Ok(value) => {
            println!("{value}");
            ExitCode::SUCCESS
        }
        Err(code) => {
            println!(
                "{}",
                json!({"schema":"vep.setup.v1","id":request_id,"seq":1,"event":"result","status":"failed","code":code})
            );
            ExitCode::from(
                if matches!(
                    code,
                    "INVALID_REQUEST"
                        | "PROTOCOL_UNSUPPORTED"
                        | "REQUEST_TOO_LARGE"
                        | "DEVELOPMENT_SANDBOX_REQUIRED"
                ) {
                    2
                } else {
                    1
                },
            )
        }
    }
}
