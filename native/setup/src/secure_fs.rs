//! Local file operations. Pin traversal directories, reject reparse points and
//! multiply-linked files, and never weaken an existing directory's security.
//! This is not a boundary against administrators or malicious current-SID code.
use crate::Result;
use std::{
    fs::{self, File, OpenOptions},
    io,
    path::{Component, Path},
};

pub struct DirectoryGuard {
    _files: Vec<File>,
}
pub struct CheckedFile {
    pub file: File,
    _parents: DirectoryGuard,
}
impl io::Read for CheckedFile {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        io::Read::read(&mut self.file, buffer)
    }
}
impl io::Write for CheckedFile {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        io::Write::write(&mut self.file, buffer)
    }
    fn flush(&mut self) -> io::Result<()> {
        io::Write::flush(&mut self.file)
    }
}

fn absolute(path: &Path) -> Result<()> {
    if !path.is_absolute() || path.components().any(|p| matches!(p, Component::ParentDir)) {
        return Err("PATH_UNSUPPORTED");
    }
    #[cfg(windows)]
    for part in path.components() {
        if let Component::Normal(name) = part {
            let name = name.to_str().ok_or("PATH_UNSUPPORTED")?;
            let base = name.split('.').next().unwrap_or("").to_ascii_uppercase();
            if name.ends_with(['.', ' '])
                || name.contains(':')
                || name.chars().any(|c| c < ' ')
                || [
                    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6",
                    "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7",
                    "LPT8", "LPT9",
                ]
                .contains(&base.as_str())
            {
                return Err("PATH_UNSUPPORTED");
            }
        }
    }
    Ok(())
}
#[cfg(windows)]
pub fn wide_path(path: &Path) -> Result<Vec<u16>> {
    use std::os::windows::ffi::OsStrExt;
    use std::path::Prefix;
    absolute(path)?;
    match path.components().next() {
        Some(Component::Prefix(p)) => match p.kind() {
            Prefix::VerbatimDisk(_) => Ok(path.as_os_str().encode_wide().chain([0]).collect()),
            Prefix::Disk(_) => Ok("\\\\?\\"
                .encode_utf16()
                .chain(path.as_os_str().encode_wide())
                .chain([0])
                .collect()),
            _ => Err("LOCAL_PATH_REQUIRED"),
        },
        _ => Err("PATH_UNSUPPORTED"),
    }
}
pub fn pin_directory(path: &Path) -> Result<DirectoryGuard> {
    absolute(path)?;
    let mut paths: Vec<_> = path.ancestors().collect();
    paths.reverse();
    let mut files = Vec::new();
    for path in paths {
        let mut options = OpenOptions::new();
        options.read(true);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            // Deny directory rename/removal, while permitting child creation and
            // atomic child replacement. This does not forbid same-SID metadata
            // changes; those are outside the ownership threat boundary.
            options
                .custom_flags(0x02000000 | 0x00200000)
                .share_mode(1 | 2);
        }
        #[cfg(not(windows))]
        if fs::symlink_metadata(path)
            .map_err(|_| "PATH_UNAVAILABLE")?
            .file_type()
            .is_symlink()
        {
            return Err("LINK_FORBIDDEN");
        }
        let file = options.open(path).map_err(|_| "PATH_BUSY_OR_UNAVAILABLE")?;
        inspect(&file, true)?;
        files.push(file);
    }
    Ok(DirectoryGuard { _files: files })
}

pub fn inspect(file: &File, directory: bool) -> Result<()> {
    let metadata = file.metadata().map_err(|_| "FILE_UNAVAILABLE")?;
    if metadata.is_dir() != directory || (!directory && !metadata.is_file()) {
        return Err("FILE_TYPE_INVALID");
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        #[repr(C)]
        struct Information {
            attributes: u32,
            times: [u32; 6],
            volume: u32,
            size_high: u32,
            size_low: u32,
            links: u32,
            index_high: u32,
            index_low: u32,
        }
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn GetFileInformationByHandle(
                handle: *mut std::ffi::c_void,
                information: *mut Information,
            ) -> i32;
        }
        let mut information: Information = unsafe { std::mem::zeroed() };
        if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
            return Err("FILE_IDENTITY_UNAVAILABLE");
        }
        if information.attributes & 0x400 != 0 {
            return Err("LINK_FORBIDDEN");
        }
        if !directory && information.links != 1 {
            return Err("HARDLINK_FORBIDDEN");
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if !directory && metadata.nlink() != 1 {
            return Err("HARDLINK_FORBIDDEN");
        }
    }
    Ok(())
}

fn open(path: &Path, new: bool, lock: bool) -> Result<CheckedFile> {
    let parents = pin_directory(path.parent().ok_or("PATH_UNSUPPORTED")?)?;
    let mut options = OpenOptions::new();
    options.read(true).write(new || lock).create_new(new);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options
            .custom_flags(0x00200000)
            .share_mode(if lock { 1 | 2 } else { 1 });
    }
    #[cfg(not(windows))]
    if !new
        && fs::symlink_metadata(path)
            .map_err(|_| "FILE_UNAVAILABLE")?
            .file_type()
            .is_symlink()
    {
        return Err("LINK_FORBIDDEN");
    }
    let file = options.open(path).map_err(|_| {
        if new {
            "STATE_WRITE_FAILED"
        } else {
            "FILE_BUSY_OR_UNAVAILABLE"
        }
    })?;
    inspect(&file, false)?;
    Ok(CheckedFile {
        file,
        _parents: parents,
    })
}
pub fn read(path: &Path) -> Result<CheckedFile> {
    open(path, false, false)
}
pub fn create_new(path: &Path) -> Result<CheckedFile> {
    open(path, true, false)
}
pub fn lock_file(path: &Path) -> Result<CheckedFile> {
    open(path, false, true)
}

pub fn create_dir_all(path: &Path) -> Result<()> {
    absolute(path)?;
    let mut paths: Vec<_> = path.ancestors().collect();
    paths.reverse();
    let mut guards = Vec::new();
    for path in paths {
        if !path.exists() {
            let _parent = pin_directory(path.parent().ok_or("PATH_UNSUPPORTED")?)?;
            match fs::create_dir(path) {
                Ok(()) => {}
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
                Err(_) => return Err("STORAGE_REQUIRED"),
            }
        }
        guards.push(pin_directory(path)?);
    }
    Ok(())
}
pub fn rename(from: &Path, to: &Path) -> Result<()> {
    let _source = pin_directory(from.parent().ok_or("PATH_UNSUPPORTED")?)?;
    let _target = pin_directory(to.parent().ok_or("PATH_UNSUPPORTED")?)?;
    if to.exists() {
        return Err("RELEASE_ID_CONFLICT");
    }
    fs::rename(from, to).map_err(|_| "STORAGE_REQUIRED")
}
