//! Local development transactions only. There is deliberately no production
//! root selection, network/update service, signature bypass, GC or data deletion.
mod candidate;
mod operation;
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
    #[serde(default)]
    events: bool,
}

fn execute(root: PathBuf, request: &Request, operation: &operation::Operation) -> Result<Value> {
    if request.schema != "vep.setup.v1" || request.id.is_empty() || request.id.len() > 128 {
        return Err("PROTOCOL_UNSUPPORTED");
    }
    if request.op == "capabilities" {
        return Ok(
            json!({"development_only":true,"max_requests":1,"network":false,
            "operations":["init-sandbox","plan","apply","status","rollback","repair"],
            "production_install":false,"bootstrap_state":"vep.bootstrap-state.v1", "streaming_events":true,"cancel":"same-id stdin cancel; pre-commit only"}),
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
                sandbox.apply_with(
                    &input,
                    request.plan_id.as_deref().ok_or("INVALID_REQUEST")?,
                    operation,
                )
            }
        }
        "rollback" => sandbox.rollback_with(
            request.release_id.as_deref().ok_or("INVALID_REQUEST")?,
            request.plan_id.as_deref().ok_or("INVALID_REQUEST")?,
            operation,
        ),
        "repair" => sandbox.repair(),
        _ => Err("OPERATION_UNSUPPORTED"),
    }
}

fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let mut request_id = None;
    let mut operation = None;
    let outcome = (|| -> Result<Value> {
        if args.len() < 3 || args[0] != "--sandbox-root" {
            return Err("DEVELOPMENT_SANDBOX_REQUIRED");
        }
        let root = PathBuf::from(&args[1]);
        if args[2] == "run" || args[2] == "run-profile" {
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
            let open_start = std::time::Instant::now();
            let sandbox = state::Sandbox::open(root)?;
            let profile = args[2] == "run-profile";
            if profile {
                eprintln!(
                    "{}",
                    json!({"schema":"vep.launch-profile.v1","scope":"sandbox_open","elapsed_ms":open_start.elapsed().as_secs_f64()*1000.0})
                );
            }
            let code = sandbox.launch(release, &rest[1..], profile)?;
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
        if request.id.is_empty() || request.id.len() > 128 {
            return Err("INVALID_REQUEST");
        }
        request_id = Some(request.id.clone());
        let active_operation = operation::Operation::new(request.id.clone(), request.events);
        active_operation.listen();
        operation = Some(active_operation.clone());
        execute(root, &request, &active_operation)
    })();
    let (fields, exit) = match outcome {
        Ok(value) => (json!({"status":"ok","value":value}), 0),
        Err(code) => (
            json!({"status":if code=="CANCELLED" {"cancelled"} else {"failed"},"code":code}),
            if code == "CANCELLED" {
                3
            } else if matches!(
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
        ),
    };
    let output = if let Some(operation) = operation {
        operation.finish(fields)
    } else {
        let mut value = json!({"schema":"vep.setup.v1","id":request_id,"seq":1,"event":"result"});
        value
            .as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        use std::io::Write;
        writeln!(io::stdout().lock(), "{value}").map_err(|_| "PROTOCOL_OUTPUT_FAILED")
    };
    ExitCode::from(if output.is_err() { 1 } else { exit })
}
