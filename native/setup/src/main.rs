//! Local development transactions only. There is deliberately no production
//! root selection, network/update service, signature bypass, GC or data deletion.
mod candidate;
mod operation;
mod ownership;
mod payload;
mod secure_fs;
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
    #[serde(default)]
    confirmed: bool,
    #[serde(default)]
    accept_licenses: bool,
    #[serde(default)]
    license_offset: u32,
}
fn bundled_fields() -> Option<Value> {
    let directory = env::current_exe().ok()?.parent()?.to_path_buf();
    let bytes = payload::read(&directory.join("bundle.json"), 65536).ok()?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    if value["schema"] != "vep.offline-bundle.v1"
        || value["manifest"] != "payload.manifest.json"
        || value["payload"] != "payload"
    {
        return None;
    }
    let expected = value["manifest_sha256"].as_str()?;
    let input = payload::Input::load(
        &directory.join("payload.manifest.json"),
        &directory.join("payload"),
        expected,
    )
    .ok()?;
    input.user_contract().ok()?;
    Some(
        json!({"manifest":directory.join("payload.manifest.json"),"payload":directory.join("payload"),"manifest_sha256":expected}),
    )
}
fn default_user_root() -> Result<PathBuf> {
    if let Ok(executable) = env::current_exe() {
        if let Some(bin) = executable.parent() {
            if bin.file_name().is_some_and(|n| n == "bin")
                && bin
                    .parent()
                    .and_then(|p| p.file_name())
                    .is_some_and(|n| n == "app")
            {
                if let Some(root) = bin.parent().and_then(|p| p.parent()) {
                    return Ok(root.to_path_buf());
                }
            }
        }
    }
    ownership::default_root()
}

fn execute(
    root: PathBuf,
    user: bool,
    request: &Request,
    operation: &operation::Operation,
) -> Result<Value> {
    if request.schema != "vep.setup.v1" || request.id.is_empty() || request.id.len() > 128 {
        return Err("PROTOCOL_UNSUPPORTED");
    }
    if request.op == "capabilities" {
        let fields = if user {
            bundled_fields().or_else(|| {
                state::Sandbox::open_user(root.clone())
                    .ok()?
                    .current_fields()
                    .ok()
            })
        } else {
            None
        };
        return Ok(
            json!({"development_only":!user,"max_requests":1,"network":false,
            "operations":["init-sandbox","init-user","paths","plan","apply","status","rollback","repair"],
                "production_install":user,"distribution_ready":false,"publisher_authenticated":false,"resolved_root":root,"bundle":fields,"token":ownership::token_status().ok(),"bootstrap_state":"vep.bootstrap-state.v1", "streaming_events":true,"cancel":"same-id stdin cancel; pre-commit only"}),
        );
    }
    if request.op == "licenses" {
        let input = payload::Input::load(
            request.manifest.as_deref().ok_or("INVALID_REQUEST")?,
            request.payload.as_deref().ok_or("INVALID_REQUEST")?,
            request
                .manifest_sha256
                .as_deref()
                .ok_or("INVALID_REQUEST")?,
        )?;
        return input.license_page(request.license_offset);
    }
    if request.op == "paths" {
        return Ok(
            json!({"root":root,"default_root":ownership::default_root()?,"mode":if user {"user"} else {"sandbox"},"token":ownership::token_status()?,"creates_files":false}),
        );
    }
    if request.op == "init-sandbox" {
        if user {
            return Err("OPERATION_UNSUPPORTED");
        }
        state::Sandbox::initialize(&root)?;
    }
    if request.op == "init-user" {
        if !user || !request.confirmed {
            return Err("CONFIRMATION_REQUIRED");
        }
        state::Sandbox::initialize_user(&root)?;
    }
    let sandbox = if user {
        state::Sandbox::open_user(root)?
    } else {
        state::Sandbox::open(root)?
    };
    match request.op.as_str() {
        "init-sandbox" | "init-user" | "status" => sandbox.status(),
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
                if user && !request.accept_licenses {
                    return Err("LICENSE_ACCEPTANCE_REQUIRED");
                }
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
    let mut args: Vec<_> = env::args_os().skip(1).collect();
    if args.first().is_some_and(|v| v == "--help" || v == "-h") || args.is_empty() {
        println!(
            "VEP offline Windows x64 installer candidate (unsigned)\nvep-setup [--user | --user-root ABSOLUTE_PATH | --sandbox-root ABSOLUTE_PATH] COMMAND\nCommands: paths, initialize --confirm, status, repair, plan/apply --manifest FILE --payload DIR --sha256 HASH [--plan-id ID --accept-licenses], rollback --release-id ID --plan-id ID, run -- VEP_ARGS\nJSONL interface: <root selection> --protocol=jsonl\nNo elevation, online update, PATH/registry changes or data deletion."
        );
        return ExitCode::SUCCESS;
    }
    // Installed launcher stays usable after the external bundle is removed.
    if env::current_exe()
        .ok()
        .and_then(|p| {
            p.file_stem()
                .map(|s| s.to_string_lossy().to_ascii_lowercase())
        })
        .as_deref()
        == Some("vep")
    {
        let executable = env::current_exe().unwrap();
        if let Some(root) = executable
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
        {
            let mut launch = vec![
                "--user-root".into(),
                root.as_os_str().to_owned(),
                "run".into(),
                "--".into(),
            ];
            launch.extend(args);
            args = launch;
        }
    }
    if args.first().is_some_and(|v| v == "--user") {
        match default_user_root() {
            Ok(root) => {
                args.remove(0);
                args.insert(0, root.into_os_string());
                args.insert(0, "--user-root".into());
            }
            Err(code) => {
                eprintln!("{code}");
                return ExitCode::from(1);
            }
        }
    } else if args
        .first()
        .is_none_or(|v| v != "--user-root" && v != "--sandbox-root")
    {
        match default_user_root() {
            Ok(root) => {
                args.insert(0, root.into_os_string());
                args.insert(0, "--user-root".into());
            }
            Err(code) => {
                eprintln!("{code}");
                return ExitCode::from(1);
            }
        }
    }
    let mut request_id = None;
    let mut operation = None;
    let outcome = (|| -> Result<Value> {
        if args.len() < 3 || (args[0] != "--sandbox-root" && args[0] != "--user-root") {
            return Err("DEVELOPMENT_SANDBOX_REQUIRED");
        }
        let root = PathBuf::from(&args[1]);
        let user = args[0] == "--user-root";
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
            let sandbox = if user {
                state::Sandbox::open_user(root)?
            } else {
                state::Sandbox::open(root)?
            };
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
        let mut bytes = Vec::new();
        if args[2] == "--protocol=jsonl" {
            if args.len() != 3 {
                return Err("PROTOCOL_UNSUPPORTED");
            }
            io::stdin()
                .lock()
                .take(65537)
                .read_until(b'\n', &mut bytes)
                .map_err(|_| "INVALID_REQUEST")?;
        } else {
            let op = match args[2].to_str().ok_or("INVALID_REQUEST")? {
                "initialize" => {
                    if user {
                        "init-user"
                    } else {
                        "init-sandbox"
                    }
                }
                v => v,
            };
            let mut value = json!({"schema":"vep.setup.v1","id":"cli","op":op});
            let mut index = 3;
            while index < args.len() {
                let flag = args[index].to_str().ok_or("INVALID_REQUEST")?;
                if flag == "--confirm" || flag == "--accept-licenses" {
                    value[if flag == "--confirm" {
                        "confirmed"
                    } else {
                        "accept_licenses"
                    }] = json!(true);
                    index += 1;
                    continue;
                }
                let key = match flag {
                    "--manifest" => "manifest",
                    "--payload" => "payload",
                    "--sha256" => "manifest_sha256",
                    "--plan-id" => "plan_id",
                    "--release-id" => "release_id",
                    "--license-offset" => "license_offset",
                    _ => return Err("INVALID_REQUEST"),
                };
                if !value[key].is_null() {
                    return Err("INVALID_REQUEST");
                }
                value[key] = json!(
                    args.get(index + 1)
                        .and_then(|s| s.to_str())
                        .ok_or("INVALID_REQUEST")?
                );
                if key == "license_offset" {
                    value[key] = json!(
                        value[key]
                            .as_str()
                            .ok_or("INVALID_REQUEST")?
                            .parse::<u32>()
                            .map_err(|_| "INVALID_REQUEST")?
                    );
                }
                index += 2;
            }
            bytes = serde_json::to_vec(&value).map_err(|_| "INVALID_REQUEST")?;
        }
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
        execute(root, user, &request, &active_operation)
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
