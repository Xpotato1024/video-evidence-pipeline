#![cfg_attr(windows, windows_subsystem = "windows")]
#[path = "../gui_client.rs"]
mod gui_client;
#[cfg(windows)]
#[path = "../gui_windows.rs"]
mod gui_windows;

fn main() {
    #[cfg(windows)]
    gui_windows::show();
    #[cfg(not(windows))]
    eprintln!("GUI_UNSUPPORTED_TARGET: development GUI currently supports Windows only");
}
