//! Comparison fixture only: local, unsigned input; never a production installer.
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, env, fs, io::{Read, Write}, path::{Path, PathBuf}, process::{Command, ExitCode}};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Entry { path: String, size: u64, sha256: String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest { schema: String, files: Vec<Entry> }

fn safe_relative(value: &str) -> bool {
    !value.is_empty() && value.len() <= 240 && value.split('/').all(|part| {
        !part.is_empty() && part != "." && part != ".." && !part.ends_with('.')
            && part.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            && !["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"].contains(&part.split('.').next().unwrap().to_ascii_uppercase().as_str())
    })
}
fn plain(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|_| "PATH_UNAVAILABLE")?;
    if meta.file_type().is_symlink() { return Err("LINK_FORBIDDEN".into()); }
    #[cfg(windows)] { use std::os::windows::fs::MetadataExt; if meta.file_attributes() & 0x400 != 0 { return Err("LINK_FORBIDDEN".into()); } }
    Ok(())
}
fn run(args: &[String]) -> Result<serde_json::Value, String> {
    if args.len() == 1 && args[0] == "capabilities" { return Ok(json!({"schema":"vep.poc.v1","operations":["capabilities","verify","extract","launch"]})); }
    if args.len() == 3 && args[0] == "launch" {
        let status = Command::new(&args[1]).args(["-I", "-S", &args[2]]).status().map_err(|_| "LAUNCH_FAILED")?;
        if !status.success() { return Err("WORKER_FAILED".into()); }
        return Ok(json!({"ok":true}));
    }
    if !((args.len() == 3 && args[0] == "verify") || (args.len() == 4 && args[0] == "extract")) { return Err("INVALID_REQUEST".into()); }
    if fs::metadata(&args[1]).map_err(|_| "MANIFEST_INVALID")?.len() > 65536 { return Err("MANIFEST_INVALID".into()); }
    let bytes = fs::read(&args[1]).map_err(|_| "MANIFEST_INVALID")?;
    let manifest: Manifest = serde_json::from_slice(&bytes).map_err(|_| "MANIFEST_INVALID")?;
    if manifest.schema != "vep.poc.v1" || manifest.files.is_empty() || manifest.files.len() > 4096 { return Err("MANIFEST_INVALID".into()); }
    let root = Path::new(&args[2]); plain(root)?;
    let mut seen = BTreeSet::new(); let mut total = 0;
    for entry in &manifest.files {
        if !safe_relative(&entry.path) || !seen.insert(entry.path.to_ascii_lowercase()) || entry.size > 64*1024*1024 || entry.sha256.len() != 64 || !entry.sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) { return Err("MANIFEST_INVALID".into()); }
        total += entry.size; if total > 256*1024*1024 { return Err("MANIFEST_INVALID".into()); }
    }
    let destination = if args[0] == "extract" { let p=PathBuf::from(&args[3]); fs::create_dir(&p).map_err(|_| "DESTINATION_EXISTS")?; Some(p) } else { None };
    for entry in &manifest.files {
        let mut path=root.to_path_buf(); for part in entry.path.split('/') { path.push(part); plain(&path)?; }
        let mut input=fs::File::open(&path).map_err(|_| "PAYLOAD_INVALID")?;
        if !input.metadata().map_err(|_| "PAYLOAD_INVALID")?.is_file() || input.metadata().unwrap().len()!=entry.size { return Err("PAYLOAD_INVALID".into()); }
        let mut output = if let Some(dest)=&destination { let p=dest.join(&entry.path); fs::create_dir_all(p.parent().unwrap()).map_err(|_| "COPY_FAILED")?; Some(fs::OpenOptions::new().write(true).create_new(true).open(p).map_err(|_| "COPY_FAILED")?) } else {None};
        let mut hasher=Sha256::new(); let mut count=0; let mut buffer=[0u8;65536];
        loop { let n=input.read(&mut buffer).map_err(|_| "PAYLOAD_INVALID")?; if n==0 {break;} count+=n as u64; if count>entry.size {return Err("PAYLOAD_INVALID".into());} hasher.update(&buffer[..n]); if let Some(out)=&mut output {out.write_all(&buffer[..n]).map_err(|_| "COPY_FAILED")?;} }
        if count!=entry.size || format!("{:x}",hasher.finalize())!=entry.sha256 { return Err("PAYLOAD_INVALID".into()); }
    }
    Ok(json!({"bytes":total,"files":manifest.files.len(),"manifest_sha256":format!("{:x}",Sha256::digest(&bytes)),"ok":true}))
}
fn main() -> ExitCode { match run(&env::args().skip(1).collect::<Vec<_>>()) {Ok(value)=>{println!("{value}"); ExitCode::SUCCESS},Err(code)=>{println!("{}",json!({"ok":false,"code":code}));ExitCode::FAILURE}} }
