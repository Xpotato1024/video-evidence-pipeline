use crate::{Result, operation::Operation};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Instant,
};

pub fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn safe_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && relative(value)
        && !value.contains('/')
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
pub fn relative(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 240
        && value.split('/').count() <= 64
        && value.split('/').all(|part| {
            if part.is_empty()
                || part == "."
                || part == ".."
                || part.ends_with('.')
                || !part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            {
                return false;
            }
            let base = part.split('.').next().unwrap().to_ascii_uppercase();
            ![
                "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7",
                "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8",
                "LPT9",
            ]
            .contains(&base.as_str())
        })
}
pub fn no_links(path: &Path) -> Result<()> {
    let mut current = PathBuf::new();
    for part in path.components() {
        current.push(part);
        if let Ok(meta) = fs::symlink_metadata(&current) {
            if meta.file_type().is_symlink() {
                return Err("LINK_FORBIDDEN");
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                if meta.file_attributes() & 0x400 != 0 {
                    return Err("LINK_FORBIDDEN");
                }
            }
        } else if current.exists() {
            return Err("PATH_UNAVAILABLE");
        }
    }
    Ok(())
}
pub fn read(path: &Path, limit: u64) -> Result<Vec<u8>> {
    no_links(path)?;
    let file = crate::secure_fs::read(path)?;
    let meta = file.file.metadata().map_err(|_| "FILE_UNAVAILABLE")?;
    if !meta.is_file() || meta.len() > limit {
        return Err("FILE_LIMIT_EXCEEDED");
    }
    let mut bytes = Vec::new();
    file.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "FILE_UNAVAILABLE")?;
    if bytes.len() as u64 > limit {
        return Err("FILE_LIMIT_EXCEEDED");
    }
    Ok(bytes)
}
#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub path: String,
    pub size: u64,
    pub sha256: String,
}
#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema: String,
    pub release_id: String,
    pub target: String,
    pub python: String,
    pub files: Vec<Entry>,
}

pub struct Input {
    pub manifest: Manifest,
    pub bytes: Vec<u8>,
    pub sha256: String,
    pub root: PathBuf,
    _guard: crate::secure_fs::DirectoryGuard,
}
fn directories(manifest: &Manifest) -> Result<BTreeSet<String>> {
    let mut result = BTreeSet::new();
    for entry in &manifest.files {
        let mut current = String::new();
        let parts: Vec<_> = entry.path.split('/').collect();
        for part in &parts[..parts.len() - 1] {
            if !current.is_empty() {
                current.push('/');
            }
            current.push_str(part);
            result.insert(current.clone());
            if result.len() > 32768 {
                return Err("PAYLOAD_LIMIT_EXCEEDED");
            }
        }
    }
    Ok(result)
}
impl Input {
    pub fn license_page(&self, offset: u32) -> Result<serde_json::Value> {
        self.user_contract()?;
        let path = "licenses/THIRD_PARTY_NOTICES.txt";
        let entry = self
            .manifest
            .files
            .iter()
            .find(|e| e.path == path)
            .ok_or("USER_PAYLOAD_INCOMPLETE")?;
        let bytes = read(&self.root.join(path), 8 * 1024 * 1024)?;
        if digest(&bytes) != entry.sha256 || bytes.len() as u64 != entry.size {
            return Err("PAYLOAD_HASH_MISMATCH");
        }
        let text = std::str::from_utf8(&bytes).map_err(|_| "LICENSE_TEXT_INVALID")?;
        let total = text.chars().count();
        if offset as usize > total {
            return Err("LICENSE_RANGE_INVALID");
        }
        let page: String = text.chars().skip(offset as usize).take(8000).collect();
        let next = offset as usize + page.chars().count();
        Ok(
            serde_json::json!({"text":page,"offset":offset,"next_offset":if next<total {next} else {0},"total_characters":total,"sha256":entry.sha256,"license_index":"licenses/index.json","license_acceptance_required":true}),
        )
    }
    pub fn user_contract(&self) -> Result<()> {
        for name in [
            "tools/vep-setup.exe",
            "tools/vep-setup-gui.exe",
            "licenses/THIRD_PARTY_NOTICES.txt",
            "licenses/index.json",
            "SBOM.spdx.json",
            "source/vep-source.zip",
        ] {
            if !self.manifest.files.iter().any(|entry| entry.path == name) {
                return Err("USER_PAYLOAD_INCOMPLETE");
            }
        }
        let path = "SBOM.spdx.json";
        let entry = self
            .manifest
            .files
            .iter()
            .find(|e| e.path == path)
            .ok_or("USER_PAYLOAD_INCOMPLETE")?;
        let bytes = read(&self.root.join(path), 8 * 1024 * 1024)?;
        if digest(&bytes) != entry.sha256 {
            return Err("PAYLOAD_HASH_MISMATCH");
        }
        let sbom: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| "SBOM_INVALID")?;
        if sbom["spdxVersion"] != "SPDX-2.3" {
            return Err("SBOM_INVALID");
        }
        let files = sbom["files"].as_array().ok_or("SBOM_INVALID")?;
        if files.len() > 16384 {
            return Err("SBOM_INVALID");
        }
        let mut inventory = std::collections::BTreeMap::new();
        for file in files {
            let name = file["fileName"]
                .as_str()
                .ok_or("SBOM_INVALID")?
                .strip_prefix("./")
                .ok_or("SBOM_INVALID")?;
            if !relative(name) {
                return Err("SBOM_INVALID");
            }
            let hashes = file["checksums"].as_array().ok_or("SBOM_INVALID")?;
            let sha = hashes
                .iter()
                .find(|h| h["algorithm"] == "SHA256")
                .and_then(|h| h["checksumValue"].as_str())
                .ok_or("SBOM_INVALID")?;
            if inventory.insert(name, sha).is_some() {
                return Err("SBOM_INVALID");
            }
        }
        if inventory.len() != self.manifest.files.len() - 1
            || self
                .manifest
                .files
                .iter()
                .filter(|e| e.path != "SBOM.spdx.json")
                .any(|entry| {
                    inventory.get(entry.path.as_str()).copied() != Some(entry.sha256.as_str())
                })
        {
            return Err("SBOM_INVENTORY_MISMATCH");
        }
        Ok(())
    }
    pub fn load(manifest_path: &Path, root: &Path, expected: &str) -> Result<Self> {
        let bytes = read(manifest_path, 8 * 1024 * 1024)?;
        if digest(&bytes) != expected {
            return Err("MANIFEST_HASH_MISMATCH");
        }
        let manifest: Manifest = serde_json::from_slice(&bytes).map_err(|_| "MANIFEST_INVALID")?;
        let expected_target = format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH);
        if manifest.schema != "vep.sandbox-release.v1"
            || !safe_id(&manifest.release_id)
            || manifest.target != expected_target
            || !relative(&manifest.python)
            || !manifest.python.starts_with("runtime/")
            || manifest.files.is_empty()
            || manifest.files.len() > 16384
        {
            return Err("MANIFEST_INVALID");
        }
        let mut seen = BTreeSet::new();
        let mut total = 0u64;
        for entry in &manifest.files {
            if !relative(&entry.path)
                || !seen.insert(entry.path.to_ascii_lowercase())
                || entry.size > 256 * 1024 * 1024
                || entry.sha256.len() != 64
                || !entry
                    .sha256
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err("MANIFEST_INVALID");
            }
            total = total
                .checked_add(entry.size)
                .ok_or("PAYLOAD_LIMIT_EXCEEDED")?;
            if total > 1024 * 1024 * 1024 {
                return Err("PAYLOAD_LIMIT_EXCEEDED");
            }
        }
        for required in [
            &manifest.python,
            "payload/bootstrap.py",
            "payload/LICENSE",
            "payload/uv.lock",
            "payload/.agents/skill-system.toml",
        ] {
            if !manifest.files.iter().any(|f| f.path == required) {
                return Err("PAYLOAD_INCOMPLETE");
            }
        }
        let dirs = directories(&manifest)?;
        let mut folded = BTreeSet::new();
        for dir in dirs {
            if seen.contains(&dir.to_ascii_lowercase()) || !folded.insert(dir.to_ascii_lowercase())
            {
                return Err("MANIFEST_INVALID");
            }
        }
        no_links(root)?;
        if !root.is_absolute() || !root.is_dir() {
            return Err("PAYLOAD_UNAVAILABLE");
        }
        Ok(Self {
            manifest,
            sha256: digest(&bytes),
            bytes,
            root: root.to_path_buf(),
            _guard: crate::secure_fs::pin_directory(root)?,
        })
    }
    pub fn verify_copy_with(
        &self,
        destination: Option<&Path>,
        operation: &Operation,
    ) -> Result<()> {
        // Inventory bounds the only files used by the isolated worker. Unlisted
        // input files are never copied. Immutable release dirs are checked below.
        let phase = if destination.is_some() {
            "STAGE"
        } else {
            "VERIFY"
        };
        operation.progress(phase, 0, self.manifest.files.len() as u64)?;
        for (index, entry) in self.manifest.files.iter().enumerate() {
            operation.check()?;
            let source = self.root.join(&entry.path);
            no_links(&source)?;
            let mut input = crate::secure_fs::read(&source)?;
            let meta = input.file.metadata().map_err(|_| "PAYLOAD_UNAVAILABLE")?;
            if !meta.is_file() || meta.len() != entry.size {
                return Err("PAYLOAD_HASH_MISMATCH");
            }
            let mut output = if let Some(root) = destination {
                let path = root.join(&entry.path);
                no_links(&path)?;
                crate::secure_fs::create_dir_all(path.parent().unwrap())?;
                Some(crate::secure_fs::create_new(&path)?)
            } else {
                None
            };
            let mut buffer = [0u8; 65536];
            let mut count = 0;
            let mut hash = Sha256::new();
            loop {
                operation.check()?;
                let n = input.read(&mut buffer).map_err(|_| "PAYLOAD_UNAVAILABLE")?;
                if n == 0 {
                    break;
                }
                count += n as u64;
                if count > entry.size {
                    return Err("PAYLOAD_HASH_MISMATCH");
                }
                hash.update(&buffer[..n]);
                if let Some(file) = &mut output {
                    file.write_all(&buffer[..n])
                        .map_err(|_| "STORAGE_REQUIRED")?;
                }
            }
            if count != entry.size || format!("{:x}", hash.finalize()) != entry.sha256 {
                return Err("PAYLOAD_HASH_MISMATCH");
            }
            if let Some(file) = output {
                crate::secure_fs::inspect(&file.file, false)?;
                file.file.sync_all().map_err(|_| "STORAGE_REQUIRED")?;
            }
            if (index + 1) % 64 == 0 || index + 1 == self.manifest.files.len() {
                operation.progress(phase, (index + 1) as u64, self.manifest.files.len() as u64)?;
            }
        }
        Ok(())
    }
    pub fn verify_installed_with(&self, operation: &Operation) -> Result<()> {
        self.verify_installed_profile_with(operation).map(|_| ())
    }
    pub fn verify_installed_profile_with(&self, operation: &Operation) -> Result<(f64, f64)> {
        let hash_start = Instant::now();
        self.verify_copy_with(None, operation)?;
        let hash_ms = hash_start.elapsed().as_secs_f64() * 1000.0;
        let inventory_start = Instant::now();
        operation.progress("INVENTORY", 0, self.manifest.files.len() as u64)?;
        let expected: BTreeSet<_> = self
            .manifest
            .files
            .iter()
            .map(|f| f.path.clone())
            .chain(["release.json".to_string()])
            .collect();
        let expected_dirs = directories(&self.manifest)?;
        fn walk(
            root: &Path,
            dir: &Path,
            found: &mut BTreeSet<String>,
            expected_dirs: &BTreeSet<String>,
            operation: &Operation,
        ) -> Result<()> {
            for child in fs::read_dir(dir).map_err(|_| "PAYLOAD_UNAVAILABLE")? {
                operation.check()?;
                let path = child.map_err(|_| "PAYLOAD_UNAVAILABLE")?.path();
                no_links(&path)?;
                let relative = path
                    .strip_prefix(root)
                    .unwrap()
                    .to_str()
                    .ok_or("PATH_UNSUPPORTED")?
                    .replace('\\', "/");
                if path.is_dir() {
                    if !expected_dirs.contains(&relative) {
                        return Err("PAYLOAD_INVENTORY_MISMATCH");
                    }
                    walk(root, &path, found, expected_dirs, operation)?;
                } else if path.is_file() {
                    found.insert(relative);
                } else {
                    return Err("PAYLOAD_INVALID");
                }
                if found.len() > 16385 {
                    return Err("PAYLOAD_LIMIT_EXCEEDED");
                }
            }
            Ok(())
        }
        let mut found = BTreeSet::new();
        walk(
            &self.root,
            &self.root,
            &mut found,
            &expected_dirs,
            operation,
        )?;
        if found != expected {
            return Err("PAYLOAD_INVENTORY_MISMATCH");
        }
        Ok((hash_ms, inventory_start.elapsed().as_secs_f64() * 1000.0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn storage_paths_reject_escape_aliases_and_unbounded_depth() {
        for value in [
            "../x",
            "/x",
            "C:/x",
            "a\\b",
            "a/CON.txt",
            "a/x:stream",
            "a/../b",
            "a/trailing.",
            "a//b",
        ] {
            assert!(!relative(value), "{value}");
        }
        assert!(!relative(&vec!["a"; 65].join("/")));
        assert!(relative("payload/.agents/skills/x/SKILL.md"));
    }
    #[test]
    fn manifest_parser_rejects_duplicate_keys_and_unknown_fields() {
        assert!(
            serde_json::from_str::<Entry>(r#"{"path":"a","path":"b","size":0,"sha256":"x"}"#)
                .is_err()
        );
        assert!(
            serde_json::from_str::<Entry>(r#"{"path":"a","size":0,"sha256":"x","extra":true}"#)
                .is_err()
        );
    }
}
