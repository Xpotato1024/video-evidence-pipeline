use crate::{
    Result,
    payload::{self, Input},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const MARKER: &str = "VEP DEVELOPMENT SANDBOX v1\nNO PRIVATE USER DATA\n";
#[derive(Serialize, Deserialize, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
struct Active {
    schema: String,
    generation: u64,
    receipt_id: Option<String>,
    receipt_sha256: Option<String>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    schema: String,
    release_id: String,
    manifest_sha256: String,
    context_sha256: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    schema: String,
    old: Active,
    new: Active,
    phase: String,
}
pub struct Sandbox {
    root: PathBuf,
}

fn unique() -> String {
    format!(
        "{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}
fn write_new(path: &Path, bytes: &[u8]) -> Result<()> {
    payload::no_links(path)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| "STATE_WRITE_FAILED")?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|_| "STATE_WRITE_FAILED")
}
fn replace(path: &Path, bytes: &[u8]) -> Result<()> {
    payload::no_links(path)?;
    let temp = path.with_extension(format!("{}.tmp", unique()));
    write_new(&temp, bytes)?;
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn MoveFileExW(old: *const u16, new: *const u16, flags: u32) -> i32;
        }
        let old: Vec<u16> = temp.as_os_str().encode_wide().chain([0]).collect();
        let new: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        if unsafe { MoveFileExW(old.as_ptr(), new.as_ptr(), 1 | 8) } == 0 {
            return Err("ACTIVATION_IO_FAILED");
        }
    }
    #[cfg(not(windows))]
    {
        fs::rename(temp, path).map_err(|_| "ACTIVATION_IO_FAILED")?;
        File::open(path.parent().unwrap())
            .and_then(|d| d.sync_all())
            .map_err(|_| "ACTIVATION_IO_FAILED")?;
    }
    Ok(())
}
fn bytes<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| "STATE_INTEGRITY_FAILED")
}
fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    serde_json::from_slice(&payload::read(path, 8 * 1024 * 1024)?)
        .map_err(|_| "STATE_INTEGRITY_FAILED")
}
fn interrupt(point: &str) {
    #[cfg(feature = "fault-injection")]
    if std::env::var("VEP_SANDBOX_INTERRUPT").as_deref() == Ok(point) {
        std::process::exit(86);
    }
    let _ = point;
}

impl Sandbox {
    pub fn initialize(root: &Path) -> Result<()> {
        if !root.is_absolute() || !root.is_dir() {
            return Err("EMPTY_SANDBOX_REQUIRED");
        }
        payload::no_links(root)?;
        if fs::read_dir(root)
            .map_err(|_| "EMPTY_SANDBOX_REQUIRED")?
            .next()
            .is_some()
        {
            return Err("EMPTY_SANDBOX_REQUIRED");
        }
        write_new(&root.join(".vep-development-only"), MARKER.as_bytes())?;
        for part in [
            "app/versions",
            "app/staging",
            "state/receipts",
            "state/contexts",
            "state/transactions",
            "state/bindings",
            "data",
        ] {
            fs::create_dir_all(root.join(part)).map_err(|_| "STORAGE_REQUIRED")?;
        }
        let active = Active {
            schema: "vep.bootstrap-state.v1".into(),
            generation: 0,
            receipt_id: None,
            receipt_sha256: None,
        };
        write_new(&root.join("state/active.json"), &bytes(&active)?)?;
        for name in ["writer", "selection"] {
            write_new(&root.join("state").join(format!("{name}.lock")), b"")?;
        }
        Ok(())
    }
    pub fn open(root: PathBuf) -> Result<Self> {
        if !root.is_absolute() {
            return Err("DEVELOPMENT_SANDBOX_REQUIRED");
        }
        payload::no_links(&root)?;
        if payload::read(&root.join(".vep-development-only"), 1024)? != MARKER.as_bytes() {
            return Err("DEVELOPMENT_SANDBOX_REQUIRED");
        }
        for part in [
            "app/versions",
            "app/staging",
            "state/receipts",
            "state/contexts",
            "state/transactions",
            "state/bindings",
            "data",
        ] {
            let path = root.join(part);
            payload::no_links(&path)?;
            if !path.is_dir() {
                return Err("STATE_INTEGRITY_FAILED");
            }
        }
        Ok(Self { root })
    }
    fn lock(&self, name: &str) -> Result<File> {
        let path = self.root.join("state").join(format!("{name}.lock"));
        payload::no_links(&path)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(|_| "STATE_LOCK_FAILED")?;
        file.try_lock().map_err(|_| "OPERATION_IN_PROGRESS")?;
        Ok(file)
    }
    fn active(&self) -> Result<Active> {
        let path = self.root.join("state/active.json");
        if !path.exists() {
            return Err("RECOVERY_REQUIRED");
        }
        let state: Active = read_json(&path)?;
        if state.schema != "vep.bootstrap-state.v1" {
            return Err("STATE_SCHEMA_UNSUPPORTED");
        }
        if state.generation == 0 && state.receipt_id.is_none() && state.receipt_sha256.is_none() {
            return Ok(state);
        }
        if state.generation == 0 || state.receipt_id.is_none() || state.receipt_sha256.is_none() {
            return Err("STATE_INTEGRITY_FAILED");
        }
        let id = state.receipt_id.as_deref().unwrap();
        if !payload::safe_id(id) {
            return Err("STATE_INTEGRITY_FAILED");
        }
        let receipt = payload::read(
            &self.root.join("state/receipts").join(format!("{id}.json")),
            65536,
        )?;
        if Some(payload::digest(&receipt)) != state.receipt_sha256 {
            return Err("STATE_INTEGRITY_FAILED");
        }
        let receipt: Receipt =
            serde_json::from_slice(&receipt).map_err(|_| "STATE_INTEGRITY_FAILED")?;
        if receipt.schema != "vep.receipt.v1" || receipt.release_id != id {
            return Err("STATE_INTEGRITY_FAILED");
        }
        Ok(state)
    }
    fn require_clean_journals(&self) -> Result<()> {
        for path in fs::read_dir(self.root.join("state/transactions"))
            .map_err(|_| "STATE_INTEGRITY_FAILED")?
        {
            let path = path.map_err(|_| "STATE_INTEGRITY_FAILED")?.path();
            if path.extension().is_some_and(|ext| ext == "json") {
                let journal: Journal = read_json(&path)?;
                if journal.schema != "vep.sandbox-journal.v1" {
                    return Err("STATE_SCHEMA_UNSUPPORTED");
                }
                if journal.phase == "prepared" {
                    return Err("RECOVERY_REQUIRED");
                }
                if !matches!(journal.phase.as_str(), "committed" | "not-committed") {
                    return Err("STATE_INTEGRITY_FAILED");
                }
            }
        }
        Ok(())
    }
    pub fn status(&self) -> Result<Value> {
        let _lock = self.lock("selection")?;
        Ok(json!({"development_only":true,"active":self.active()?}))
    }
    fn plan_for(&self, input: &Input, active: &Active) -> Result<Value> {
        let body = json!({"schema":"vep.sandbox-plan.v1","root":self.root,"old_generation":active.generation,"release_id":input.manifest.release_id,"manifest_sha256":input.sha256});
        let id = payload::digest(&bytes(&body)?);
        Ok(
            json!({"plan_id":id,"plan":body,"expanded_bytes":input.manifest.files.iter().map(|f|f.size).sum::<u64>(),"publisher_authenticated":false,"development_only":true}),
        )
    }
    pub fn plan(&self, input: &Input) -> Result<Value> {
        let _lock = self.lock("selection")?;
        self.plan_for(input, &self.active()?)
    }
    pub fn apply(&self, input: &Input, expected_plan: &str) -> Result<Value> {
        let _writer = self.lock("writer")?;
        self.require_clean_journals()?;
        let old = {
            let _selection = self.lock("selection")?;
            self.active()?
        };
        if self.plan_for(input, &old)?["plan_id"] != expected_plan {
            return Err("PLAN_STALE");
        }
        let id = &input.manifest.release_id;
        let version = self.root.join("app/versions").join(id);
        let tx = unique();
        let stage = self.root.join("app/staging").join(&tx);
        if version.exists() {
            let installed = Input::load(&version.join("release.json"), &version, &input.sha256)
                .map_err(|_| "RELEASE_ID_CONFLICT")?;
            installed.verify_installed()?;
        } else {
            fs::create_dir(&stage).map_err(|_| "STORAGE_REQUIRED")?;
            input.verify_copy(Some(&stage))?;
            interrupt("after-copy");
            write_new(&stage.join("release.json"), &input.bytes)?;
            let staged = Input::load(&stage.join("release.json"), &stage, &input.sha256)?;
            staged.verify_installed()?;
            let candidate_context = self
                .root
                .join("state/contexts")
                .join(format!("candidate-{tx}.json"));
            write_new(
                &candidate_context,
                &bytes(
                    &json!({"schema":"vep.runtime-context.v1","release_id":id,"resource_root":stage.join("payload"),"data_root":self.root.join("data"),"bindings_root":self.root.join("state/bindings"),"app_root":self.root.join("app"),"state_root":self.root.join("state")}),
                )?,
            )?;
            let mut check = Command::new(stage.join(&input.manifest.python))
                .args(["-I", "-B", "-S", "-X", "utf8"])
                .arg(stage.join("payload/bootstrap.py"))
                .arg(candidate_context)
                .args(["doctor", "--json"])
                .env_remove("PYTHONPATH")
                .env_remove("PYTHONHOME")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|_| "CANDIDATE_CHECK_FAILED")?;
            let start = Instant::now();
            loop {
                match check.try_wait() {
                    Ok(Some(status)) => {
                        if !status.success() {
                            return Err("CANDIDATE_CHECK_FAILED");
                        }
                        break;
                    }
                    Ok(None) => {}
                    Err(_) => {
                        let _ = check.kill();
                        let _ = check.wait();
                        return Err("CANDIDATE_CHECK_FAILED");
                    }
                }
                if start.elapsed() > Duration::from_secs(60) {
                    let _ = check.kill();
                    let _ = check.wait();
                    return Err("CANDIDATE_CHECK_TIMEOUT");
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            fs::rename(&stage, &version).map_err(|_| "STORAGE_REQUIRED")?;
            interrupt("after-promote");
        }
        let context = json!({"schema":"vep.runtime-context.v1","release_id":id,"resource_root":version.join("payload"),"data_root":self.root.join("data"),"bindings_root":self.root.join("state/bindings"),"app_root":self.root.join("app"),"state_root":self.root.join("state")});
        let context_bytes = bytes(&context)?;
        let context_path = self.root.join("state/contexts").join(format!("{id}.json"));
        if context_path.exists() {
            if payload::read(&context_path, 65536)? != context_bytes {
                return Err("RELEASE_ID_CONFLICT");
            }
        } else {
            write_new(&context_path, &context_bytes)?;
        }
        let receipt = Receipt {
            schema: "vep.receipt.v1".into(),
            release_id: id.clone(),
            manifest_sha256: input.sha256.clone(),
            context_sha256: payload::digest(&context_bytes),
        };
        let receipt_bytes = bytes(&receipt)?;
        let receipt_path = self.root.join("state/receipts").join(format!("{id}.json"));
        if receipt_path.exists() {
            if payload::read(&receipt_path, 65536)? != receipt_bytes {
                return Err("RELEASE_ID_CONFLICT");
            }
        } else {
            write_new(&receipt_path, &receipt_bytes)?;
        }
        self.activate(old, id, payload::digest(&receipt_bytes), &tx)
    }
    fn activate(&self, old: Active, id: &str, receipt_sha256: String, tx: &str) -> Result<Value> {
        let _selection = self.lock("selection")?;
        if self.active()? != old {
            return Err("PLAN_STALE");
        }
        let new = Active {
            schema: old.schema.clone(),
            generation: old
                .generation
                .checked_add(1)
                .ok_or("STATE_INTEGRITY_FAILED")?,
            receipt_id: Some(id.into()),
            receipt_sha256: Some(receipt_sha256),
        };
        let path = self
            .root
            .join("state/transactions")
            .join(format!("{tx}.json"));
        let mut journal = Journal {
            schema: "vep.sandbox-journal.v1".into(),
            old,
            new: new.clone(),
            phase: "prepared".into(),
        };
        write_new(&path, &bytes(&journal)?)?;
        interrupt("prepared");
        let activation = replace(&self.root.join("state/active.json"), &bytes(&new)?);
        interrupt("after-replace");
        let observed = self.active().map_err(|_| "RECOVERY_REQUIRED")?;
        if observed == new {
            journal.phase = "committed".into();
            let journal_saved = replace(&path, &bytes(&journal)?).is_ok();
            interrupt("committed");
            Ok(
                json!({"active":new,"recovery_pending":!journal_saved,"activation_warning":activation.is_err(),"development_only":true}),
            )
        } else if observed == journal.old {
            Err("ACTIVATION_NOT_COMMITTED")
        } else {
            Err("RECOVERY_REQUIRED")
        }
    }
    fn installed(&self, id: &str) -> Result<(Input, Receipt)> {
        if !payload::safe_id(id) {
            return Err("INVALID_REQUEST");
        }
        let receipt: Receipt =
            read_json(&self.root.join("state/receipts").join(format!("{id}.json")))?;
        if receipt.schema != "vep.receipt.v1" || receipt.release_id != id {
            return Err("STATE_INTEGRITY_FAILED");
        }
        let root = self.root.join("app/versions").join(id);
        let input = Input::load(&root.join("release.json"), &root, &receipt.manifest_sha256)?;
        input.verify_installed()?;
        if payload::digest(&payload::read(
            &self.root.join("state/contexts").join(format!("{id}.json")),
            65536,
        )?) != receipt.context_sha256
        {
            return Err("STATE_INTEGRITY_FAILED");
        }
        Ok((input, receipt))
    }
    pub fn rollback(&self, id: &str, expected_plan: &str) -> Result<Value> {
        let _writer = self.lock("writer")?;
        self.require_clean_journals()?;
        let (input, _) = self.installed(id)?;
        let old = {
            let _selection = self.lock("selection")?;
            self.active()?
        };
        if self.plan_for(&input, &old)?["plan_id"] != expected_plan {
            return Err("PLAN_STALE");
        }
        let receipt = payload::read(
            &self.root.join("state/receipts").join(format!("{id}.json")),
            65536,
        )?;
        self.activate(old, id, payload::digest(&receipt), &unique())
    }
    pub fn repair(&self) -> Result<Value> {
        let _writer = self.lock("writer")?;
        let _selection = self.lock("selection")?;
        let active = self.active()?;
        let mut recovered = 0;
        for path in fs::read_dir(self.root.join("state/transactions"))
            .map_err(|_| "STATE_INTEGRITY_FAILED")?
        {
            let path = path.map_err(|_| "STATE_INTEGRITY_FAILED")?.path();
            if path.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let mut journal: Journal = read_json(&path)?;
            if journal.schema != "vep.sandbox-journal.v1" {
                return Err("STATE_SCHEMA_UNSUPPORTED");
            }
            if journal.phase == "prepared" {
                journal.phase = if active == journal.new {
                    "committed"
                } else if active == journal.old {
                    "not-committed"
                } else {
                    return Err("RECOVERY_REQUIRED");
                }
                .into();
                replace(&path, &bytes(&journal)?)?;
                recovered += 1;
            }
        }
        Ok(
            json!({"active":active,"reconciled_journals":recovered,"data_changed":false,"stages_retained":true}),
        )
    }
    pub fn launch(&self, release: Option<&str>, args: &[OsString]) -> Result<i32> {
        let id = {
            let _selection = self.lock("selection")?;
            let active = self.active()?;
            let id = release
                .or(active.receipt_id.as_deref())
                .ok_or("NOT_INSTALLED")?;
            id.to_string()
        };
        // All versions are retained, so verification needs no selection lock.
        let (input, _receipt) = self.installed(&id)?;
        // Versions are never deleted in this phase. GC/lease handoff is gated
        // before any future deletion; updates cannot replace an existing ID.
        let context = self
            .root
            .join("state/contexts")
            .join(format!("{}.json", input.manifest.release_id));
        let status = Command::new(input.root.join(&input.manifest.python))
            .args(["-I", "-B", "-S", "-X", "utf8"])
            .arg(input.root.join("payload/bootstrap.py"))
            .arg(context)
            .args(args)
            .env_remove("PYTHONPATH")
            .env_remove("PYTHONHOME")
            .status()
            .map_err(|_| "WORKER_LAUNCH_FAILED")?;
        Ok(status.code().unwrap_or(1))
    }
}
