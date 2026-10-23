//! A candidate is uncommitted. Never leave its descendants running on cancellation.
use crate::Result;
use std::process::{Child, Command};

pub struct Candidate {
    pub child: Child,
    #[cfg(windows)]
    job: isize,
}

#[cfg(windows)]
mod windows {
    #[repr(C)]
    #[derive(Default)]
    pub struct BasicLimits {
        process_time: i64,
        job_time: i64,
        flags: u32,
        minimum: usize,
        maximum: usize,
        active_processes: u32,
        affinity: usize,
        priority: u32,
        scheduling: u32,
    }
    #[repr(C)]
    #[derive(Default)]
    pub struct Limits {
        basic: BasicLimits,
        io: [u64; 6],
        process_memory: usize,
        job_memory: usize,
        peak_process_memory: usize,
        peak_job_memory: usize,
    }
    #[repr(C)]
    #[derive(Default)]
    pub struct Thread {
        size: u32,
        usage: u32,
        pub id: u32,
        pub owner: u32,
        base: i32,
        delta: i32,
        flags: u32,
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        pub fn CreateJobObjectW(attributes: *const u8, name: *const u16) -> isize;
        pub fn SetInformationJobObject(
            job: isize,
            kind: u32,
            data: *const Limits,
            size: u32,
        ) -> i32;
        pub fn AssignProcessToJobObject(job: isize, process: isize) -> i32;
        pub fn CloseHandle(handle: isize) -> i32;
        fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> isize;
        fn Thread32First(snapshot: isize, entry: *mut Thread) -> i32;
        fn Thread32Next(snapshot: isize, entry: *mut Thread) -> i32;
        fn OpenThread(access: u32, inherit: i32, id: u32) -> isize;
        fn ResumeThread(thread: isize) -> u32;
    }
    pub fn limits() -> Limits {
        let mut limits = Limits::default();
        limits.basic.flags = 0x2000;
        limits
    }
    pub fn resume(pid: u32) -> bool {
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(4, 0);
            if snapshot == -1 {
                return false;
            }
            let mut entry = Thread {
                size: std::mem::size_of::<Thread>() as u32,
                ..Default::default()
            };
            let mut available = Thread32First(snapshot, &mut entry);
            let mut resumed = false;
            while available != 0 {
                if entry.owner == pid {
                    let thread = OpenThread(2, 0, entry.id);
                    if thread != 0 {
                        resumed = ResumeThread(thread) != u32::MAX;
                        CloseHandle(thread);
                    }
                    break;
                }
                available = Thread32Next(snapshot, &mut entry);
            }
            CloseHandle(snapshot);
            resumed
        }
    }
}

impl Candidate {
    pub fn spawn(command: &mut Command) -> Result<Self> {
        #[cfg(windows)]
        {
            use std::os::windows::{io::AsRawHandle, process::CommandExt};
            let job = unsafe { windows::CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if job == 0 {
                return Err("CANDIDATE_CONTAINMENT_FAILED");
            }
            let limits = windows::limits();
            if unsafe {
                windows::SetInformationJobObject(
                    job,
                    9,
                    &limits,
                    std::mem::size_of_val(&limits) as u32,
                )
            } == 0
            {
                unsafe {
                    windows::CloseHandle(job);
                }
                return Err("CANDIDATE_CONTAINMENT_FAILED");
            }
            command.creation_flags(4 | 0x08000000); // suspended, no console
            let child = match command.spawn() {
                Ok(child) => child,
                Err(_) => {
                    unsafe {
                        windows::CloseHandle(job);
                    }
                    return Err("CANDIDATE_CHECK_FAILED");
                }
            };
            let mut candidate = Self { child, job };
            if unsafe {
                windows::AssignProcessToJobObject(job, candidate.child.as_raw_handle() as isize)
            } == 0
                || !windows::resume(candidate.child.id())
            {
                let _ = candidate.child.kill();
                return Err("CANDIDATE_CONTAINMENT_FAILED");
            }
            Ok(candidate)
        }
        #[cfg(not(windows))]
        {
            Ok(Self {
                child: command.spawn().map_err(|_| "CANDIDATE_CHECK_FAILED")?,
            })
        }
    }
}
impl Drop for Candidate {
    fn drop(&mut self) {
        #[cfg(windows)]
        unsafe {
            windows::CloseHandle(self.job);
        } // kills all owned descendants
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
