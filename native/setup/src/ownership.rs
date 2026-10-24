//! Windows per-user ownership checks. Existing ACLs are inspected, never repaired.
use crate::Result;
use std::{
    fs::File,
    path::{Path, PathBuf},
};

#[cfg(windows)]
mod windows {
    use super::*;
    use std::{
        ffi::{OsString, c_void},
        os::windows::{
            ffi::OsStringExt,
            io::AsRawHandle,
        },
    };
    type Handle = *mut c_void;
    #[repr(C)]
    struct Guid {
        a: u32,
        b: u16,
        c: u16,
        d: [u8; 8],
    }
    #[repr(C)]
    struct SecurityAttributes {
        length: u32,
        descriptor: Handle,
        inherit: i32,
    }
    #[repr(C)]
    struct TokenUser {
        sid: Handle,
        attributes: u32,
    }
    #[repr(C)]
    struct AclInfo {
        count: u32,
        used: u32,
        free: u32,
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetCurrentProcess() -> Handle;
        fn CloseHandle(handle: isize) -> i32;
        fn LocalFree(memory: Handle) -> Handle;
        fn CreateDirectoryW(path: *const u16, attributes: *const SecurityAttributes) -> i32;
        fn GetDriveTypeW(root: *const u16) -> u32;
        fn GetVolumeInformationW(
            root: *const u16,
            name: *mut u16,
            name_size: u32,
            serial: *mut u32,
            maximum: *mut u32,
            flags: *mut u32,
            filesystem: *mut u16,
            filesystem_size: u32,
        ) -> i32;
    }
    #[link(name = "advapi32")]
    unsafe extern "system" {
        fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
        fn GetTokenInformation(
            token: Handle,
            class: u32,
            data: Handle,
            size: u32,
            needed: *mut u32,
        ) -> i32;
        fn ConvertSidToStringSidW(sid: Handle, text: *mut *mut u16) -> i32;
        fn ConvertStringSidToSidW(text: *const u16, sid: *mut Handle) -> i32;
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text: *const u16,
            revision: u32,
            descriptor: *mut Handle,
            size: *mut u32,
        ) -> i32;
        fn GetSecurityInfo(
            handle: Handle,
            kind: u32,
            information: u32,
            owner: *mut Handle,
            group: *mut Handle,
            dacl: *mut Handle,
            sacl: *mut Handle,
            descriptor: *mut Handle,
        ) -> u32;
        fn GetSecurityDescriptorControl(
            descriptor: Handle,
            control: *mut u16,
            revision: *mut u32,
        ) -> i32;
        fn GetAclInformation(acl: Handle, information: Handle, size: u32, class: u32) -> i32;
        fn GetAce(acl: Handle, index: u32, ace: *mut Handle) -> i32;
        fn EqualSid(a: Handle, b: Handle) -> i32;
        fn CheckTokenMembership(token: Handle, sid: Handle, member: *mut i32) -> i32;
    }
    #[link(name = "shell32")]
    unsafe extern "system" {
        fn SHGetKnownFolderPath(
            folder: *const Guid,
            flags: u32,
            token: Handle,
            path: *mut *mut u16,
        ) -> i32;
    }
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(memory: Handle);
    }
    struct Local(Handle);
    impl Drop for Local {
        fn drop(&mut self) {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
    struct Token(Handle);
    impl Drop for Token {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0 as isize);
            }
        }
    }
    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain([0]).collect()
    }
    fn token() -> Result<Token> {
        let mut result = std::ptr::null_mut();
        if unsafe { OpenProcessToken(GetCurrentProcess(), 8, &mut result) } == 0 {
            return Err("TOKEN_UNAVAILABLE");
        }
        Ok(Token(result))
    }
    fn user(token: &Token) -> Result<Vec<u8>> {
        let mut needed = 0;
        unsafe {
            GetTokenInformation(token.0, 1, std::ptr::null_mut(), 0, &mut needed);
        }
        if needed == 0 || needed > 65536 {
            return Err("TOKEN_UNAVAILABLE");
        }
        let mut buffer = vec![0u8; needed as usize];
        if unsafe {
            GetTokenInformation(token.0, 1, buffer.as_mut_ptr().cast(), needed, &mut needed)
        } == 0
        {
            return Err("TOKEN_UNAVAILABLE");
        }
        Ok(buffer)
    }
    fn sid(buffer: &[u8]) -> Handle {
        unsafe { std::ptr::read_unaligned(buffer.as_ptr().cast::<TokenUser>()).sid }
    }
    fn system_sid(value: &str) -> Result<Local> {
        let mut pointer = std::ptr::null_mut();
        if unsafe { ConvertStringSidToSidW(wide(value).as_ptr(), &mut pointer) } == 0 {
            return Err("TOKEN_UNAVAILABLE");
        }
        Ok(Local(pointer))
    }
    pub fn token_status() -> Result<serde_json::Value> {
        let token = token()?;
        let mut elevated = 0u32;
        let mut needed = 0;
        if unsafe {
            GetTokenInformation(
                token.0,
                20,
                (&mut elevated as *mut u32).cast(),
                4,
                &mut needed,
            )
        } == 0
        {
            return Err("TOKEN_UNAVAILABLE");
        }
        let admin = system_sid("S-1-5-32-544")?;
        let mut enabled = 0;
        if unsafe { CheckTokenMembership(std::ptr::null_mut(), admin.0, &mut enabled) } == 0 {
            return Err("TOKEN_UNAVAILABLE");
        }
        Ok(
            serde_json::json!({"elevated":elevated!=0,"administrators_enabled":enabled!=0,"credential_changes":false}),
        )
    }
    pub fn standard_token() -> Result<()> {
        let status = token_status()?;
        if status["elevated"] != false || status["administrators_enabled"] != false {
            return Err("UNELEVATED_USER_REQUIRED");
        }
        Ok(())
    }
    pub fn default_root() -> Result<PathBuf> {
        let folder = Guid {
            a: 0xf1b32785,
            b: 0x6fba,
            c: 0x4fcf,
            d: [0x9d, 0x55, 0x7b, 0x8e, 0x7f, 0x15, 0x70, 0x91],
        };
        let mut path: *mut u16 = std::ptr::null_mut();
        if unsafe { SHGetKnownFolderPath(&folder, 0, std::ptr::null_mut(), &mut path) } < 0
            || path.is_null()
        {
            return Err("USER_ROOT_UNAVAILABLE");
        }
        let mut length = 0;
        unsafe {
            while *path.add(length) != 0 {
                length += 1;
                if length > 32767 {
                    CoTaskMemFree(path.cast());
                    return Err("USER_ROOT_UNAVAILABLE");
                }
            }
        }
        let result = PathBuf::from(OsString::from_wide(unsafe {
            std::slice::from_raw_parts(path, length)
        }))
        .join("VEP");
        unsafe {
            CoTaskMemFree(path.cast());
        }
        Ok(result)
    }
    pub fn local_ntfs(path: &Path) -> Result<()> {
        use std::path::{Component, Prefix};
        let drive = match path.components().next() {
            Some(Component::Prefix(p)) => match p.kind() {
                Prefix::Disk(d) | Prefix::VerbatimDisk(d) => d,
                _ => return Err("LOCAL_NTFS_REQUIRED"),
            },
            _ => return Err("LOCAL_NTFS_REQUIRED"),
        };
        let root = wide(&format!("{}:\\", drive as char));
        let mut filesystem = [0u16; 32];
        if unsafe { GetDriveTypeW(root.as_ptr()) } != 3
            || unsafe {
                GetVolumeInformationW(
                    root.as_ptr(),
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    filesystem.as_mut_ptr(),
                    32,
                )
            } == 0
        {
            return Err("LOCAL_NTFS_REQUIRED");
        }
        let name = String::from_utf16_lossy(
            &filesystem[..filesystem.iter().position(|v| *v == 0).unwrap_or(32)],
        );
        if name != "NTFS" {
            return Err("LOCAL_NTFS_REQUIRED");
        }
        Ok(())
    }
    pub fn verify_file(file: &File, protected: bool) -> Result<()> {
        let token = token()?;
        let buffer = user(&token)?;
        let system = system_sid("S-1-5-18")?;
        let admins = system_sid("S-1-5-32-544")?;
        let mut owner = std::ptr::null_mut();
        let mut dacl = std::ptr::null_mut();
        let mut descriptor = std::ptr::null_mut();
        if unsafe {
            GetSecurityInfo(
                file.as_raw_handle(),
                1,
                1 | 4,
                &mut owner,
                std::ptr::null_mut(),
                &mut dacl,
                std::ptr::null_mut(),
                &mut descriptor,
            )
        } != 0
        {
            return Err("ROOT_PRIVACY_UNVERIFIED");
        }
        let descriptor = Local(descriptor);
        if owner.is_null() || dacl.is_null() || unsafe { EqualSid(owner, sid(&buffer)) } == 0 {
            return Err("ROOT_PRIVACY_UNVERIFIED");
        }
        let mut control = 0u16;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(descriptor.0, &mut control, &mut revision) } == 0
            || (protected && control & 0x1000 == 0)
        {
            return Err("ROOT_PRIVACY_UNVERIFIED");
        }
        let mut info: AclInfo = unsafe { std::mem::zeroed() };
        if unsafe {
            GetAclInformation(
                dacl,
                (&mut info as *mut AclInfo).cast(),
                std::mem::size_of::<AclInfo>() as u32,
                2,
            )
        } == 0
            || info.count == 0
            || info.count > 64
        {
            return Err("ROOT_PRIVACY_UNVERIFIED");
        }
        for index in 0..info.count {
            let mut ace: Handle = std::ptr::null_mut();
            if unsafe { GetAce(dacl, index, &mut ace) } == 0 || ace.is_null() {
                return Err("ROOT_PRIVACY_UNVERIFIED");
            }
            let header = ace.cast::<u8>();
            let size = unsafe { std::ptr::read_unaligned(header.add(2).cast::<u16>()) };
            // Only uncomplicated allow ACEs for the current SID and OS admins.
            // Unknown/conditional/object/deny ACEs are not guessed effective.
            if unsafe { *header } != 0 || size < 16 {
                return Err("ROOT_PRIVACY_UNVERIFIED");
            }
            let count = unsafe { *header.add(9) } as u16;
            if unsafe { *header.add(8) } != 1 || count > 15 || 16 + 4 * count > size {
                return Err("ROOT_PRIVACY_UNVERIFIED");
            }
            let principal = unsafe { header.add(8).cast() };
            if [sid(&buffer), system.0, admins.0]
                .iter()
                .all(|allowed| unsafe { EqualSid(principal, *allowed) } == 0)
            {
                return Err("ROOT_PRIVACY_UNVERIFIED");
            }
        }
        Ok(())
    }
    pub fn create_private(path: &Path) -> Result<()> {
        standard_token()?;
        local_ntfs(path)?;
        let _parents = crate::secure_fs::pin_directory(path.parent().ok_or("PATH_UNSUPPORTED")?)?;
        let token = token()?;
        let buffer = user(&token)?;
        let mut text = std::ptr::null_mut();
        if unsafe { ConvertSidToStringSidW(sid(&buffer), &mut text) } == 0 {
            return Err("TOKEN_UNAVAILABLE");
        }
        let text = Local(text.cast());
        let mut length = 0;
        unsafe {
            while *text.0.cast::<u16>().add(length) != 0 {
                length += 1;
            }
        }
        let user = String::from_utf16_lossy(unsafe {
            std::slice::from_raw_parts(text.0.cast::<u16>(), length)
        });
        let sddl = wide(&format!(
            "O:{user}D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        ));
        let mut descriptor = std::ptr::null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err("ROOT_PRIVACY_UNVERIFIED");
        }
        let descriptor = Local(descriptor);
        let attributes = SecurityAttributes {
            length: std::mem::size_of::<SecurityAttributes>() as u32,
            descriptor: descriptor.0,
            inherit: 0,
        };
        let path = crate::secure_fs::wide_path(path)?;
        if unsafe { CreateDirectoryW(path.as_ptr(), &attributes) } == 0 {
            return Err("ROOT_CREATE_FAILED");
        }
        Ok(())
    }
}

#[cfg(windows)]
pub use windows::*;
#[cfg(not(windows))]
pub fn default_root() -> Result<PathBuf> {
    Err("PLATFORM_UNSUPPORTED")
}
#[cfg(not(windows))]
pub fn standard_token() -> Result<()> {
    Err("PLATFORM_UNSUPPORTED")
}
#[cfg(not(windows))]
pub fn token_status() -> Result<serde_json::Value> {
    Err("PLATFORM_UNSUPPORTED")
}
#[cfg(not(windows))]
pub fn local_ntfs(_path: &Path) -> Result<()> {
    Err("PLATFORM_UNSUPPORTED")
}
#[cfg(not(windows))]
pub fn create_private(_path: &Path) -> Result<()> {
    Err("PLATFORM_UNSUPPORTED")
}
#[cfg(not(windows))]
pub fn verify_file(_file: &File, _protected: bool) -> Result<()> {
    Err("PLATFORM_UNSUPPORTED")
}

pub fn verify_root(path: &Path) -> Result<crate::secure_fs::DirectoryGuard> {
    local_ntfs(path)?;
    let guard = crate::secure_fs::pin_directory(path)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(0x02000000 | 0x00200000)
            .share_mode(1)
            .open(path)
            .map_err(|_| "ROOT_PRIVACY_UNVERIFIED")?;
        verify_file(&file, true)?;
    }
    Ok(guard)
}
