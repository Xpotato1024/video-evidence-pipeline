use crate::{
    Result,
    operation::Operation,
    payload::{self, Input},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const MARKER: &str = "VEP DEVELOPMENT SANDBOX v1\nNO PRIVATE USER DATA\n";
const USER_MARKER: &str = ".vep-user-install.json";
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct UserMarker {
    schema: String,
    installation_id: String,
    root: PathBuf,
    phase: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BootstrapReceipt {
    schema: String,
    release_id: String,
    manifest_sha256: String,
}
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
    _guards: Vec<crate::secure_fs::DirectoryGuard>,
    user: bool,
    installation_id: Option<String>,
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
    let mut file = crate::secure_fs::create_new(path)?;
    file.write_all(bytes)
        .and_then(|_| file.file.sync_all())
        .map_err(|_| "STATE_WRITE_FAILED")
}
fn replace(path: &Path, bytes: &[u8]) -> Result<()> {
    let _parent = crate::secure_fs::pin_directory(path.parent().ok_or("PATH_UNSUPPORTED")?)?;
    payload::no_links(path)?;
    if path.exists() {
        let checked = crate::secure_fs::read(path)?;
        crate::secure_fs::inspect(&checked.file, false)?;
    }
    let temp = path.with_extension(format!("{}.tmp", unique()));
    write_new(&temp, bytes)?;
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn MoveFileExW(old: *const u16, new: *const u16, flags: u32) -> i32;
        }
        let old = crate::secure_fs::wide_path(&temp)?;
        let new = crate::secure_fs::wide_path(path)?;
        if unsafe { MoveFileExW(old.as_ptr(), new.as_ptr(), 1 | 8) } == 0 {
            #[cfg(feature = "fault-injection")]
            eprintln!(
                "MoveFileExW winerror={:?}",
                std::io::Error::last_os_error().raw_os_error()
            );
            return Err("ACTIVATION_IO_FAILED");
        }
    }
    #[cfg(not(windows))]
    {
        fs::rename(temp, path).map_err(|_| "ACTIVATION_IO_FAILED")?;
        std::fs::File::open(path.parent().unwrap())
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
    pub fn initialize_user(root: &Path) -> Result<()> {
        crate::ownership::standard_token()?;
        #[cfg(windows)]
        crate::secure_fs::wide_path(root)?;
        crate::ownership::local_ntfs(root)?;
        if !root.exists() {
            // Publish a new root only after its complete identity is durable.
            // Interrupted preparation leaves an unreferenced private sibling;
            // it never leaves a truncated marker at the selected final root.
            let parent = root.parent().ok_or("PATH_UNSUPPORTED")?;
            let _parent = crate::secure_fs::pin_directory(parent)?;
            let temporary = parent.join(format!(".vep-initialization-{}", unique()));
            crate::ownership::create_private(&temporary)?;
            let marker = UserMarker {
                schema: "vep.user-install.v1".into(),
                installation_id: unique(),
                root: fs::canonicalize(parent)
                    .map_err(|_| "ROOT_UNAVAILABLE")?
                    .join(root.file_name().ok_or("PATH_UNSUPPORTED")?),
                phase: "initializing".into(),
            };
            write_new(&temporary.join(USER_MARKER), &bytes(&marker)?)?;
            interrupt("user-init-before-root-publish");
            crate::secure_fs::rename(&temporary, root)?;
        }
        let _guard = crate::ownership::verify_root(root)?;
        let marker_path = root.join(USER_MARKER);
        if !marker_path.exists() {
            // Even an empty private directory is not ours to adopt. Claiming a
            // preexisting unmarked root races another initializer and obscures
            // crash ownership. New roots are published with a complete marker.
            return Err("FOREIGN_INSTALLATION_ROOT");
        }
        let lock_path = root.join(".initialization.lock");
        if !lock_path.exists() {
            match write_new(&lock_path, b"") {
                Ok(()) => {}
                Err(_) if lock_path.exists() => {}
                Err(code) => return Err(code),
            }
        }
        let init_lock = crate::secure_fs::lock_file(&lock_path)?;
        init_lock
            .file
            .try_lock()
            .map_err(|_| "OPERATION_IN_PROGRESS")?;
        let mut marker: UserMarker = read_json(&marker_path)?;
        Self::check_user_marker(root, &marker)?;
        if marker.phase == "ready" {
            Self::open_user(root.to_path_buf())?;
            return Ok(());
        }
        if marker.phase != "initializing" {
            return Err("STATE_SCHEMA_UNSUPPORTED");
        }
        interrupt("user-init-marker");
        for part in [
            "app/versions",
            "app/staging",
            "app/bin",
            "state/receipts",
            "state/contexts",
            "state/transactions",
            "state/bindings",
            "data",
        ] {
            crate::secure_fs::create_dir_all(&root.join(part))?;
        }
        let active = Active {
            schema: "vep.bootstrap-state.v1".into(),
            generation: 0,
            receipt_id: None,
            receipt_sha256: None,
        };
        let active_path = root.join("state/active.json");
        if active_path.exists() {
            if read_json::<Active>(&active_path)? != active {
                return Err("INITIALIZATION_CONFLICT");
            }
        } else {
            replace(&active_path, &bytes(&active)?)?;
        }
        for name in ["writer", "selection"] {
            let path = root.join("state").join(format!("{name}.lock"));
            if !path.exists() {
                write_new(&path, b"")?;
            }
        }
        interrupt("user-init-layout");
        marker.phase = "ready".into();
        replace(&marker_path, &bytes(&marker)?)?;
        Ok(())
    }
    fn check_user_marker(root: &Path, marker: &UserMarker) -> Result<()> {
        if marker.schema != "vep.user-install.v1"
            || !payload::safe_id(&marker.installation_id)
            || fs::canonicalize(root).map_err(|_| "ROOT_UNAVAILABLE")? != marker.root
        {
            return Err("ROOT_IDENTITY_MISMATCH");
        }
        Ok(())
    }
    pub fn open_user(root: PathBuf) -> Result<Self> {
        crate::ownership::standard_token()?;
        let root_guard = crate::ownership::verify_root(&root)?;
        let marker: UserMarker =
            read_json(&root.join(USER_MARKER)).map_err(|_| "USER_INSTALL_REQUIRED")?;
        Self::check_user_marker(&root, &marker)?;
        if marker.phase != "ready" {
            return Err("INITIALIZATION_INCOMPLETE");
        }
        let mut guards = vec![root_guard];
        for part in [
            "app/versions",
            "app/staging",
            "app/bin",
            "state/receipts",
            "state/contexts",
            "state/transactions",
            "state/bindings",
            "data",
        ] {
            let path = root.join(part);
            guards.push(crate::secure_fs::pin_directory(&path)?);
            Self::check_private_path(&path)?;
        }
        Self::check_private_path(&root.join(USER_MARKER))?;
        let mut pending = vec![(root.join("state"), 0usize)];
        let mut count = 0usize;
        while let Some((directory, depth)) = pending.pop() {
            if depth > 64 {
                return Err("STATE_LIMIT_EXCEEDED");
            }
            let _guard = crate::secure_fs::pin_directory(&directory)?;
            for entry in fs::read_dir(&directory).map_err(|_| "STATE_INTEGRITY_FAILED")? {
                let path = entry.map_err(|_| "STATE_INTEGRITY_FAILED")?.path();
                payload::no_links(&path)?;
                Self::check_private_path(&path)?;
                count += 1;
                if count > 65536 {
                    return Err("STATE_LIMIT_EXCEEDED");
                }
                if path.is_dir() {
                    pending.push((path, depth + 1));
                }
            }
        }
        Ok(Self {
            root,
            _guards: guards,
            user: true,
            installation_id: Some(marker.installation_id),
        })
    }
    fn check_private_path(path: &Path) -> Result<()> {
        let checked = crate::secure_fs::metadata(path)?;
        crate::ownership::verify_file(&checked.file, false)
    }
    fn private_inventory(&self, input: &Input, operation: &Operation) -> Result<()> {
        if self.user {
            let mut directories = std::collections::BTreeSet::new();
            for entry in &input.manifest.files {
                operation.check()?;
                let path = input.root.join(&entry.path);
                for parent in path
                    .ancestors()
                    .skip(1)
                    .take_while(|p| p.starts_with(&input.root))
                {
                    directories.insert(parent.to_path_buf());
                }
            }
            let total = (input.manifest.files.len() + directories.len() + 1) as u64;
            let mut completed = 0;
            operation.progress("CHECK", completed, total)?;
            Self::check_private_path(&input.root)?;
            completed += 1;
            for entry in &input.manifest.files {
                operation.check()?;
                Self::check_private_path(&input.root.join(&entry.path))?;
                completed += 1;
                if completed % 32 == 0 {
                    operation.progress("CHECK", completed, total)?;
                }
            }
            for path in directories {
                operation.check()?;
                Self::check_private_path(&path)?;
                completed += 1;
                if completed % 32 == 0 {
                    operation.progress("CHECK", completed, total)?;
                }
            }
            operation.progress("CHECK", completed, total)?;
        }
        Ok(())
    }
    fn runtime_root(&self) -> Result<PathBuf> {
        if self.user {
            fs::canonicalize(&self.root).map_err(|_| "ROOT_UNAVAILABLE")
        } else {
            Ok(self.root.clone())
        }
    }
    fn ensure_entrypoints(&self, candidate: Option<&Input>, operation: &Operation) -> Result<()> {
        if !self.user {
            return Ok(());
        }
        let path = self.root.join("state/bootstrap.json");
        let receipt: BootstrapReceipt = if path.exists() {
            read_json(&path)?
        } else {
            let input = candidate.ok_or("BOOTSTRAP_RECOVERY_REQUIRED")?;
            BootstrapReceipt {
                schema: "vep.bootstrap-receipt.v1".into(),
                release_id: input.manifest.release_id.clone(),
                manifest_sha256: input.sha256.clone(),
            }
        };
        if receipt.schema != "vep.bootstrap-receipt.v1" || !payload::safe_id(&receipt.release_id) {
            return Err("STATE_INTEGRITY_FAILED");
        }
        let root = self.root.join("app/versions").join(&receipt.release_id);
        let input = Input::load(&root.join("release.json"), &root, &receipt.manifest_sha256)?;
        input.user_contract()?;
        input.verify_installed_with(operation)?;
        self.private_inventory(&input, operation)?;
        // Persist recovery identity before publishing any entrypoint. A partial
        // first installation can then be repaired without an external bundle.
        if !path.exists() {
            replace(&path, &bytes(&receipt)?)?;
        }
        interrupt("bootstrap-receipt");
        let bin = self.root.join("app/bin");
        for existing in fs::read_dir(&bin).map_err(|_| "BOOTSTRAP_RECOVERY_REQUIRED")? {
            let existing = existing.map_err(|_| "BOOTSTRAP_RECOVERY_REQUIRED")?;
            if !["vep.exe", "vep-setup.exe", "vep-setup-gui.exe"]
                .iter()
                .any(|name| existing.file_name() == *name)
            {
                return Err("BOOTSTRAP_CONTENT_CONFLICT");
            }
        }
        for (name, source) in [
            ("vep.exe", "tools/vep-setup.exe"),
            ("vep-setup.exe", "tools/vep-setup.exe"),
            ("vep-setup-gui.exe", "tools/vep-setup-gui.exe"),
        ] {
            operation.check()?;
            let expected = input
                .manifest
                .files
                .iter()
                .find(|e| e.path == source)
                .ok_or("USER_PAYLOAD_INCOMPLETE")?;
            let bytes = payload::read(&root.join(source), 16 * 1024 * 1024)?;
            if payload::digest(&bytes) != expected.sha256 {
                return Err("PAYLOAD_HASH_MISMATCH");
            }
            let target = bin.join(name);
            if target.exists() {
                if payload::read(&target, 16 * 1024 * 1024)? != bytes {
                    return Err("BOOTSTRAP_CONTENT_CONFLICT");
                }
            } else {
                // Keep partial writes away from final executable names. Orphans
                // remain in private state and are never executed or overwritten.
                let temp = self
                    .root
                    .join("state")
                    .join(format!("bootstrap-{}.tmp", unique()));
                write_new(&temp, &bytes)?;
                interrupt("bootstrap-staged");
                crate::secure_fs::rename(&temp, &target)?;
                interrupt("bootstrap-published");
            }
            Self::check_private_path(&target)?;
        }
        Ok(())
    }
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
            crate::secure_fs::create_dir_all(&root.join(part))?;
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
        let mut guards = vec![crate::secure_fs::pin_directory(&root)?];
        for part in [
            "app/versions",
            "app/staging",
            "state/receipts",
            "state/contexts",
            "state/transactions",
            "state/bindings",
            "data",
        ] {
            guards.push(crate::secure_fs::pin_directory(&root.join(part))?);
        }
        Ok(Self {
            root,
            _guards: guards,
            user: false,
            installation_id: None,
        })
    }
    fn lock(&self, name: &str) -> Result<crate::secure_fs::CheckedFile> {
        let path = self.root.join("state").join(format!("{name}.lock"));
        payload::no_links(&path)?;
        let file = crate::secure_fs::lock_file(&path)?;
        file.file.try_lock().map_err(|_| "OPERATION_IN_PROGRESS")?;
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
        let recovery_required = match self.require_clean_journals() {
            Ok(()) => false,
            Err("RECOVERY_REQUIRED") => true,
            Err(code) => return Err(code),
        };
        Ok(
            json!({"development_only":!self.user,"installation_mode":if self.user {"user"} else {"sandbox"},"root":self.root,"installation_id":self.installation_id,"active":self.active()?,"recovery_required":recovery_required}),
        )
    }
    pub fn current_fields(&self) -> Result<Value> {
        let _lock = self.lock("selection")?;
        let active = self.active()?;
        let id = active.receipt_id.ok_or("NOT_INSTALLED")?;
        let receipt: Receipt =
            read_json(&self.root.join("state/receipts").join(format!("{id}.json")))?;
        let version = self.root.join("app/versions").join(id);
        Ok(
            json!({"manifest":version.join("release.json"),"payload":version,"manifest_sha256":receipt.manifest_sha256}),
        )
    }
    fn plan_for(&self, input: &Input, active: &Active) -> Result<Value> {
        if self.user {
            input.user_contract()?;
        }
        let body = json!({"schema":if self.user {"vep.user-plan.v1"} else {"vep.sandbox-plan.v1"},"root":self.root,"installation_id":self.installation_id,"old_generation":active.generation,"release_id":input.manifest.release_id,"manifest_sha256":input.sha256});
        let id = payload::digest(&bytes(&body)?);
        Ok(
            json!({"plan_id":id,"plan":body,"expanded_bytes":input.manifest.files.iter().map(|f|f.size).sum::<u64>(),"publisher_authenticated":false,"development_only":!self.user,"installation_mode":if self.user {"user"} else {"sandbox"},"signature_gate":"unsigned local candidate; not distribution approved"}),
        )
    }
    pub fn plan(&self, input: &Input) -> Result<Value> {
        let _lock = self.lock("selection")?;
        self.plan_for(input, &self.active()?)
    }
    pub fn apply_with(
        &self,
        input: &Input,
        expected_plan: &str,
        operation: &Operation,
    ) -> Result<Value> {
        operation.progress("PREPARE", 0, 0)?;
        let _writer = self.lock("writer")?;
        self.require_clean_journals()?;
        let runtime_root = self.runtime_root()?;
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
            installed.verify_installed_with(operation)?;
            self.private_inventory(&installed, operation)?;
        } else {
            crate::secure_fs::create_dir_all(&stage)?;
            input.verify_copy_with(Some(&stage), operation)?;
            interrupt("after-copy");
            write_new(&stage.join("release.json"), &input.bytes)?;
            let staged = Input::load(&stage.join("release.json"), &stage, &input.sha256)?;
            staged.verify_installed_with(operation)?;
            self.private_inventory(&staged, operation)?;
            operation.progress("CHECK", 0, 0)?;
            let candidate_context = self
                .root
                .join("state/contexts")
                .join(format!("candidate-{tx}.json"));
            write_new(
                &candidate_context,
                &bytes(
                    &json!({"schema":"vep.runtime-context.v1","release_id":id,"resource_root":if self.user {fs::canonicalize(stage.join("payload")).map_err(|_|"ROOT_UNAVAILABLE")?} else {stage.join("payload")},"data_root":runtime_root.join("data"),"bindings_root":runtime_root.join("state/bindings"),"app_root":runtime_root.join("app"),"state_root":runtime_root.join("state")}),
                )?,
            )?;
            let mut command = Command::new(if self.user {
                fs::canonicalize(stage.join(&input.manifest.python))
                    .map_err(|_| "ROOT_UNAVAILABLE")?
            } else {
                stage.join(&input.manifest.python)
            });
            command
                .args(["-I", "-B", "-S", "-X", "utf8"])
                .arg(if self.user {
                    fs::canonicalize(stage.join("payload/bootstrap.py"))
                        .map_err(|_| "ROOT_UNAVAILABLE")?
                } else {
                    stage.join("payload/bootstrap.py")
                })
                .arg(if self.user {
                    fs::canonicalize(candidate_context).map_err(|_| "ROOT_UNAVAILABLE")?
                } else {
                    candidate_context
                })
                .args(["doctor", "--json"])
                .env_remove("PYTHONPATH")
                .env_remove("PYTHONHOME")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            let mut check = crate::candidate::Candidate::spawn(&mut command)?;
            let start = Instant::now();
            loop {
                operation.check()?;
                match check.child.try_wait() {
                    Ok(Some(status)) => {
                        if !status.success() {
                            return Err("CANDIDATE_CHECK_FAILED");
                        }
                        break;
                    }
                    Ok(None) => {}
                    Err(_) => {
                        return Err("CANDIDATE_CHECK_FAILED");
                    }
                }
                if start.elapsed() > Duration::from_secs(60) {
                    return Err("CANDIDATE_CHECK_TIMEOUT");
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            drop(check);
            drop(staged);
            crate::secure_fs::rename(&stage, &version)?;
            interrupt("after-promote");
        }
        if self.user {
            let installed = Input::load(&version.join("release.json"), &version, &input.sha256)?;
            self.ensure_entrypoints(Some(&installed), operation)?;
        }
        let context = json!({"schema":"vep.runtime-context.v1","release_id":id,"resource_root":runtime_root.join("app/versions").join(id).join("payload"),"data_root":runtime_root.join("data"),"bindings_root":runtime_root.join("state/bindings"),"app_root":runtime_root.join("app"),"state_root":runtime_root.join("state")});
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
        self.activate(old, id, payload::digest(&receipt_bytes), &tx, operation)
    }
    fn activate(
        &self,
        old: Active,
        id: &str,
        receipt_sha256: String,
        tx: &str,
        operation: &Operation,
    ) -> Result<Value> {
        let _selection = self.lock("selection")?;
        if self.active()? != old {
            return Err("PLAN_STALE");
        }
        operation.commit()?;
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
                json!({"active":new,"recovery_pending":!journal_saved,"activation_warning":activation.is_err(),"development_only":!self.user}),
            )
        } else if observed == journal.old {
            Err("ACTIVATION_NOT_COMMITTED")
        } else {
            Err("RECOVERY_REQUIRED")
        }
    }
    fn installed_profile(
        &self,
        id: &str,
        operation: &Operation,
    ) -> Result<(Input, Receipt, Value)> {
        let metadata_start = Instant::now();
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
        let metadata_ms = metadata_start.elapsed().as_secs_f64() * 1000.0;
        let (hash_ms, inventory_ms) = input.verify_installed_profile_with(operation)?;
        let private_start = Instant::now();
        self.private_inventory(&input, operation)?;
        let private_ms = private_start.elapsed().as_secs_f64() * 1000.0;
        let context_start = Instant::now();
        if payload::digest(&payload::read(
            &self.root.join("state/contexts").join(format!("{id}.json")),
            65536,
        )?) != receipt.context_sha256
        {
            return Err("STATE_INTEGRITY_FAILED");
        }
        Ok((
            input,
            receipt,
            json!({"receipt_manifest_ms":metadata_ms,"all_file_hash_and_paths_ms":hash_ms,"inventory_walk_ms":inventory_ms,"private_inventory_ms":private_ms,"context_check_ms":context_start.elapsed().as_secs_f64()*1000.0}),
        ))
    }
    pub fn rollback_with(
        &self,
        id: &str,
        expected_plan: &str,
        operation: &Operation,
    ) -> Result<Value> {
        let _writer = self.lock("writer")?;
        self.require_clean_journals()?;
        let (input, _, _) = self.installed_profile(id, operation)?;
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
        self.activate(old, id, payload::digest(&receipt), &unique(), operation)
    }
    pub fn repair(&self) -> Result<Value> {
        let _writer = self.lock("writer")?;
        let _selection = self.lock("selection")?;
        let active = self.active()?;
        if self.user && self.root.join("state/bootstrap.json").exists() {
            self.ensure_entrypoints(None, &Operation::quiet())?;
        }
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
    pub fn launch(&self, release: Option<&str>, args: &[OsString], profile: bool) -> Result<i32> {
        let selection_start = Instant::now();
        let id = {
            let _selection = self.lock("selection")?;
            let active = self.active()?;
            let id = release
                .or(active.receipt_id.as_deref())
                .ok_or("NOT_INSTALLED")?;
            id.to_string()
        };
        // All versions are retained, so verification needs no selection lock.
        let selection_ms = selection_start.elapsed().as_secs_f64() * 1000.0;
        let (input, _receipt, mut timing) = self.installed_profile(&id, &Operation::quiet())?;
        // Versions are never deleted in this phase. GC/lease handoff is gated
        // before any future deletion; updates cannot replace an existing ID.
        let context = self
            .root
            .join("state/contexts")
            .join(format!("{}.json", input.manifest.release_id));
        let worker_start = Instant::now();
        let mut command = Command::new(if self.user {
            fs::canonicalize(input.root.join(&input.manifest.python))
                .map_err(|_| "ROOT_UNAVAILABLE")?
        } else {
            input.root.join(&input.manifest.python)
        });
        command
            .args(["-I", "-B", "-S", "-X", "utf8"])
            .arg(if self.user {
                fs::canonicalize(input.root.join("payload/bootstrap.py"))
                    .map_err(|_| "ROOT_UNAVAILABLE")?
            } else {
                input.root.join("payload/bootstrap.py")
            })
            .arg(if self.user {
                fs::canonicalize(context).map_err(|_| "ROOT_UNAVAILABLE")?
            } else {
                context
            })
            .args(args)
            .env_remove("PYTHONPATH")
            .env_remove("PYTHONHOME")
            .env_remove("VEP_PROFILE_LAUNCH");
        if profile {
            command.env("VEP_PROFILE_LAUNCH", "1");
        }
        let status = command.status().map_err(|_| "WORKER_LAUNCH_FAILED")?;
        if profile {
            timing["selection_ms"] = json!(selection_ms);
            timing["worker_wall_ms"] = json!(worker_start.elapsed().as_secs_f64() * 1000.0);
            eprintln!(
                "{}",
                json!({"schema":"vep.launch-profile.v1","scope":"native","timings":timing,"child_timings_are_nested":true})
            );
        }
        Ok(status.code().unwrap_or(1))
    }
}
