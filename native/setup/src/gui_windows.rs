//! Thin Win32 client: every filesystem decision is made by sibling vep-setup.
use crate::gui_client::{ConfirmedPlan, Inputs, Stream};
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    collections::BTreeMap,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{ChildStdin, Command, Stdio},
    sync::{
        Arc, Mutex,
        mpsc::{self, Receiver, SyncSender},
    },
};

type Handle = isize;
#[repr(C)]
#[derive(Default)]
struct Point {
    x: i32,
    y: i32,
}
#[repr(C)]
#[derive(Default)]
struct Rect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}
#[repr(C)]
struct MinMax {
    reserved: Point,
    maximum: Point,
    position: Point,
    minimum_track: Point,
    maximum_track: Point,
}
#[repr(C)]
#[derive(Default)]
struct Message {
    window: Handle,
    message: u32,
    wparam: usize,
    lparam: isize,
    time: u32,
    point: Point,
    private: u32,
}
#[repr(C)]
struct WindowClass {
    style: u32,
    procedure: unsafe extern "system" fn(Handle, u32, usize, isize) -> isize,
    class_extra: i32,
    window_extra: i32,
    instance: Handle,
    icon: Handle,
    cursor: Handle,
    background: Handle,
    menu: *const u16,
    name: *const u16,
}
#[link(name = "user32")]
unsafe extern "system" {
    fn RegisterClassW(class: *const WindowClass) -> u16;
    fn CreateWindowExW(
        ex: u32,
        class: *const u16,
        title: *const u16,
        style: u32,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        parent: Handle,
        menu: Handle,
        instance: Handle,
        param: *mut u8,
    ) -> Handle;
    fn DefWindowProcW(window: Handle, message: u32, wparam: usize, lparam: isize) -> isize;
    fn SetWindowLongPtrW(window: Handle, index: i32, value: isize) -> isize;
    fn GetWindowLongPtrW(window: Handle, index: i32) -> isize;
    fn GetMessageW(message: *mut Message, window: Handle, min: u32, max: u32) -> i32;
    fn TranslateMessage(message: *const Message) -> i32;
    fn DispatchMessageW(message: *const Message) -> isize;
    fn IsDialogMessageW(window: Handle, message: *mut Message) -> i32;
    fn ShowWindow(window: Handle, how: i32) -> i32;
    fn DestroyWindow(window: Handle) -> i32;
    fn PostQuitMessage(code: i32);
    fn SetTimer(window: Handle, id: usize, ms: u32, callback: usize) -> usize;
    fn SetWindowTextW(window: Handle, text: *const u16) -> i32;
    fn GetWindowTextW(window: Handle, text: *mut u16, max: i32) -> i32;
    fn SendMessageW(window: Handle, message: u32, wparam: usize, lparam: isize) -> isize;
    fn EnableWindow(window: Handle, enabled: i32) -> i32;
    fn MoveWindow(window: Handle, x: i32, y: i32, w: i32, h: i32, repaint: i32) -> i32;
    fn GetClientRect(window: Handle, rect: *mut Rect) -> i32;
    fn GetDpiForWindow(window: Handle) -> u32;
    fn LoadCursorW(instance: Handle, name: *const u16) -> Handle;
}
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetModuleHandleW(name: *const u16) -> Handle;
}
#[link(name = "gdi32")]
unsafe extern "system" {
    fn CreateFontW(
        h: i32,
        w: i32,
        esc: i32,
        orientation: i32,
        weight: i32,
        italic: u32,
        underline: u32,
        strike: u32,
        charset: u32,
        out: u32,
        clip: u32,
        quality: u32,
        pitch: u32,
        face: *const u16,
    ) -> Handle;
    fn DeleteObject(object: Handle) -> i32;
}

const ROOT: u16 = 100;
const MANIFEST: u16 = 101;
const PAYLOAD: u16 = 102;
const DIGEST: u16 = 103;
const INIT: u16 = 10;
const PLAN: u16 = 11;
const ROLLBACK: u16 = 12;
const CONFIRM: u16 = 13;
const CANCEL: u16 = 14;
const STATUS: u16 = 15;
const REPAIR: u16 = 16;
const CHECK: u16 = 17;
const DETAILS: u16 = 18;
const PROGRESS: u16 = 19;
const SUMMARY: u16 = 20;
const LICENSES: u16 = 21;
fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain([0]).collect()
}
fn text(window: Handle) -> String {
    let mut value = vec![0u16; 32769];
    let count = unsafe { GetWindowTextW(window, value.as_mut_ptr(), value.len() as i32) };
    String::from_utf16_lossy(&value[..count.max(0) as usize])
}
fn set(window: Handle, value: &str) {
    unsafe {
        SetWindowTextW(window, wide(value).as_ptr());
    }
}

enum Event {
    Line(Value),
    End(Option<i32>),
    Error(&'static str),
}
enum Pending {
    Install(ConfirmedPlan),
    Initialize(Inputs),
    Repair(Inputs),
}
#[derive(Default)]
struct Pipe {
    stdin: Option<ChildStdin>,
    id: String,
    cancel_pending: bool,
    cancel_sent: bool,
}
impl Pipe {
    fn deliver(&mut self) {
        if self.cancel_pending && !self.cancel_sent {
            if let Some(stdin) = self.stdin.as_mut() {
                let request = json!({"schema":"vep.setup.v1","id":self.id,"op":"cancel"});
                self.cancel_sent = writeln!(stdin, "{request}")
                    .and_then(|_| stdin.flush())
                    .is_ok();
            }
        }
    }
}
struct Window {
    window: Handle,
    instance: Handle,
    font: Handle,
    controls: BTreeMap<u16, Handle>,
    engine: PathBuf,
    tx: SyncSender<Event>,
    rx: Receiver<Event>,
    input: Arc<Mutex<Pipe>>,
    counter: u64,
    stream: Option<Stream>,
    operation: String,
    request_inputs: Option<Inputs>,
    rollback: bool,
    pending: Option<Pending>,
    busy: bool,
    compatible: bool,
    unknown: bool,
    user_mode: bool,
    license_offset: u32,
}
impl Window {
    fn control(&self, id: u16) -> Handle {
        self.controls[&id]
    }
    fn inputs(&self) -> Inputs {
        Inputs {
            root: text(self.control(ROOT)),
            manifest: text(self.control(MANIFEST)),
            payload: text(self.control(PAYLOAD)),
            digest: text(self.control(DIGEST)),
        }
    }
    fn details(&self, value: &Value) {
        set(
            self.control(DETAILS),
            &serde_json::to_string_pretty(value)
                .unwrap()
                .replace('\n', "\r\n"),
        );
    }
    fn invalidate(&mut self) {
        self.license_offset = 0;
        self.pending = None;
        set(
            self.control(SUMMARY),
            "Preview a new plan before confirming / 変更内容を再確認してください",
        );
        unsafe {
            SendMessageW(self.control(CHECK), 0xf1, 0, 0);
        }
        self.buttons();
    }
    fn buttons(&self) {
        for id in [
            ROOT, MANIFEST, PAYLOAD, DIGEST, INIT, PLAN, ROLLBACK, STATUS, REPAIR, LICENSES,
        ] {
            unsafe {
                EnableWindow(self.control(id), (!self.busy && self.compatible) as i32);
            }
        }
        let checked = unsafe { SendMessageW(self.control(CHECK), 0xf0, 0, 0) } == 1;
        unsafe {
            EnableWindow(
                self.control(CHECK),
                (!self.busy && self.pending.is_some()) as i32,
            );
            EnableWindow(
                self.control(CONFIRM),
                (!self.busy && self.pending.is_some() && checked) as i32,
            );
            EnableWindow(
                self.control(CANCEL),
                (self.busy && matches!(self.operation.as_str(), "apply" | "rollback")) as i32,
            );
        }
    }
    fn begin(&mut self, mut request: Value) {
        if self.busy {
            return;
        }
        let fields = self.inputs();
        self.counter += 1;
        let id = format!("gui-{}", self.counter);
        if let Ok(mut pipe) = self.input.lock() {
            *pipe = Pipe {
                id: id.clone(),
                ..Default::default()
            };
        }
        request["schema"] = json!("vep.setup.v1");
        request["id"] = json!(id);
        self.operation = request["op"].as_str().unwrap().into();
        request["events"] = json!(matches!(self.operation.as_str(), "apply" | "rollback"));
        self.request_inputs = Some(fields.clone());
        self.stream = Some(Stream {
            id,
            sequence: 0,
            final_seen: false,
            status: None,
        });
        self.busy = true;
        self.unknown = false;
        self.buttons();
        set(
            self.control(PROGRESS),
            &format!("Running: {}", self.operation),
        );
        spawn(
            self.engine.clone(),
            fields.root,
            self.user_mode,
            request,
            self.input.clone(),
            self.tx.clone(),
        );
    }
    fn cancel(&self) {
        if !self.busy || !matches!(self.operation.as_str(), "apply" | "rollback") {
            return;
        }
        if let Ok(mut pipe) = self.input.lock() {
            pipe.cancel_pending = true;
            pipe.deliver();
        }
        set(
            self.control(PROGRESS),
            "Cancellation requested; waiting for engine result / 取消の確定待ち",
        );
        unsafe {
            EnableWindow(self.control(CANCEL), 0);
        }
    }
    fn command(&mut self, id: u16, notification: u16) {
        if [ROOT, MANIFEST, PAYLOAD, DIGEST].contains(&id) && notification == 0x300 {
            self.invalidate();
            return;
        }
        if id == CANCEL {
            self.cancel();
            return;
        }
        if self.busy || !self.compatible {
            return;
        }
        match id {
            LICENSES => {
                let offset = self.license_offset;
                self.invalidate();
                let mut request = self.inputs().fields();
                request["op"] = json!("licenses");
                request["license_offset"] = json!(offset);
                self.begin(request);
            }
            CHECK => self.buttons(),
            PLAN | ROLLBACK => {
                self.invalidate();
                self.rollback = id == ROLLBACK;
                let mut request = self.inputs().fields();
                request["op"] = json!("plan");
                self.begin(request);
            }
            STATUS => {
                self.invalidate();
                self.begin(json!({"op":"status"}));
            }
            INIT | REPAIR => {
                self.invalidate();
                let inputs = self.inputs();
                set(
                    self.control(SUMMARY),
                    &format!(
                        "{}\r\nTarget: {}\r\nJobs/data: retained; no implicit migration.",
                        if id == INIT {
                            if self.user_mode {
                                "Create/resume private per-user installation (existing ACLs unchanged)"
                            } else {
                                "Initialize empty DEVELOPMENT folder"
                            }
                        } else {
                            "Repair: reconcile engine journal only"
                        },
                        inputs.root
                    ),
                );
                self.details(&json!({"action":if id==INIT {"Initialize selected installation"} else {"Reconcile existing journal and retained entrypoints using common engine"},"root":inputs.root,"data_policy":"No job/data deletion or migration; engine validates prerequisites","development_only":!self.user_mode,"distribution_ready":false}));
                self.pending = Some(if id == INIT {
                    Pending::Initialize(inputs)
                } else {
                    Pending::Repair(inputs)
                });
                set(
                    self.control(PROGRESS),
                    "Review the target and check confirmation / 対象を確認してください",
                );
                self.buttons();
            }
            CONFIRM => {
                if unsafe { SendMessageW(self.control(CHECK), 0xf0, 0, 0) } != 1 {
                    return;
                }
                let inputs = self.inputs();
                let request = match self.pending.take() {
                    Some(Pending::Install(plan)) => plan.request(&inputs),
                    Some(Pending::Initialize(original)) if inputs == original => Ok(
                        json!({"op":if self.user_mode {"init-user"} else {"init-sandbox"},"confirmed":true}),
                    ),
                    Some(Pending::Repair(original)) if inputs == original => {
                        Ok(json!({"op":"repair"}))
                    }
                    _ => Err("PLAN_STALE"),
                };
                unsafe {
                    SendMessageW(self.control(CHECK), 0xf1, 0, 0);
                }
                match request {
                    Ok(request) => self.begin(request),
                    Err(code) => set(self.control(PROGRESS), code),
                };
            }
            _ => {}
        }
    }
    fn poll(&mut self) {
        for _ in 0..16 {
            let Ok(event) = self.rx.try_recv() else {
                break;
            };
            match event {
                Event::Line(value) => {
                    if self
                        .stream
                        .as_mut()
                        .is_none_or(|stream| stream.accept(&value).is_err())
                    {
                        self.unknown = true;
                        if let Ok(mut input) = self.input.lock() {
                            input.stdin.take();
                        }
                        set(
                            self.control(PROGRESS),
                            "PROTOCOL_INVALID_EVENT; state must be checked",
                        );
                        continue;
                    }
                    match value["event"].as_str() {
                        Some("progress") => {
                            set(
                                self.control(PROGRESS),
                                &format!(
                                    "{} {}/{} — cancellable: {}",
                                    value["phase"].as_str().unwrap_or(""),
                                    value["completed"],
                                    value["total"],
                                    value["cancellable"]
                                ),
                            );
                            if value["cancellable"] == false {
                                unsafe {
                                    EnableWindow(self.control(CANCEL), 0);
                                }
                            }
                        }
                        Some("cancel_ack") => set(
                            self.control(PROGRESS),
                            "Cancel acknowledged; final result pending / 取消の確定待ち",
                        ),
                        Some("result") => {
                            self.details(&value);
                            let ok = value["status"] == "ok";
                            set(
                                self.control(PROGRESS),
                                &format!(
                                    "Result: {} {}",
                                    value["status"].as_str().unwrap_or("unknown"),
                                    value["code"].as_str().unwrap_or("")
                                ),
                            );
                            if self.operation == "status"
                                && value["value"]["recovery_required"] == true
                            {
                                set(
                                    self.control(PROGRESS),
                                    "RECOVERY_REQUIRED — inspect active state, then Repair / 状態を確認して復旧してください",
                                );
                            }
                            if self.operation == "capabilities" {
                                self.compatible = ok
                                    && value["value"]["streaming_events"] == true
                                    && value["value"]["development_only"] == !self.user_mode;
                                if self.user_mode && ok {
                                    if text(self.control(ROOT)).is_empty() {
                                        if let Some(root) = value["value"]["resolved_root"].as_str()
                                        {
                                            set(self.control(ROOT), root);
                                        }
                                    }
                                    for (id, key) in [
                                        (MANIFEST, "manifest"),
                                        (PAYLOAD, "payload"),
                                        (DIGEST, "manifest_sha256"),
                                    ] {
                                        if text(self.control(id)).is_empty() {
                                            if let Some(text) =
                                                value["value"]["bundle"][key].as_str()
                                            {
                                                set(self.control(id), text);
                                            }
                                        }
                                    }
                                }
                                if !self.compatible {
                                    set(
                                        self.control(PROGRESS),
                                        "PROTOCOL_UNSUPPORTED; no fallback",
                                    );
                                }
                            } else if self.operation == "licenses" && ok {
                                self.license_offset =
                                    value["value"]["next_offset"].as_u64().unwrap_or(0) as u32;
                                set(
                                    self.control(DETAILS),
                                    &value["value"]["text"]
                                        .as_str()
                                        .unwrap_or("")
                                        .replace('\n', "\r\n"),
                                );
                                set(
                                    self.control(SUMMARY),
                                    &format!(
                                        "License text: offset {} / {} characters\r\nSHA-256: {}\r\nPress Licenses > for the next page; preview a new plan before confirming.",
                                        value["value"]["offset"],
                                        value["value"]["total_characters"],
                                        value["value"]["sha256"].as_str().unwrap_or("")
                                    ),
                                );
                            } else if self.operation == "plan" && ok {
                                match ConfirmedPlan::new(
                                    value["value"].clone(),
                                    self.request_inputs.clone().unwrap(),
                                    self.rollback,
                                ) {
                                    Ok(plan) => {
                                        let value = &plan.value;
                                        set(
                                            self.control(SUMMARY),
                                            &format!(
                                                "{}: {}\r\nTarget: {}\r\nCurrent generation: {} | Expanded: {} bytes\r\nSHA-256: {}\r\nPlan ID: {}\r\nNetwork: none | Jobs/data: retained | Publisher: unauthenticated",
                                                if plan.rollback {
                                                    "Rollback"
                                                } else {
                                                    "Install / update"
                                                },
                                                value["plan"]["release_id"].as_str().unwrap(),
                                                value["plan"]["root"].as_str().unwrap(),
                                                value["plan"]["old_generation"],
                                                value["expanded_bytes"],
                                                value["plan"]["manifest_sha256"].as_str().unwrap(),
                                                value["plan_id"].as_str().unwrap()
                                            ),
                                        );
                                        self.pending = Some(Pending::Install(plan));
                                    }
                                    Err(code) => {
                                        self.unknown = true;
                                        set(self.control(PROGRESS), code);
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                }
                Event::Error(code) => {
                    self.unknown = true;
                    set(self.control(PROGRESS), code);
                }
                Event::End(code) => {
                    let final_seen = self
                        .stream
                        .as_ref()
                        .is_some_and(|stream| stream.final_seen && stream.exit_matches(code));
                    self.busy = false;
                    if !final_seen || self.unknown {
                        self.pending = None;
                        set(
                            self.control(PROGRESS),
                            &format!(
                                "ENGINE_OUTCOME_UNCERTAIN ({code:?}); querying state; repair may be required"
                            ),
                        );
                        if self.operation != "status" && self.operation != "capabilities" {
                            self.begin(json!({"op":"status"}));
                        }
                    }
                    self.buttons();
                }
            }
        }
    }
}

fn spawn(
    engine: PathBuf,
    root: String,
    user_mode: bool,
    request: Value,
    input: Arc<Mutex<Pipe>>,
    tx: SyncSender<Event>,
) {
    std::thread::spawn(move || {
        #[cfg(feature = "fault-injection")]
        if let Ok(delay) = std::env::var("VEP_GUI_TEST_SPAWN_DELAY_MS") {
            if let Ok(ms) = delay.parse::<u64>() {
                if ms <= 1000 {
                    std::thread::sleep(std::time::Duration::from_millis(ms));
                }
            }
        }
        use std::os::windows::process::CommandExt;
        let mut command = Command::new(engine);
        if user_mode && root.is_empty() {
            command.arg("--user");
        } else {
            command.args([
                if user_mode {
                    "--user-root"
                } else {
                    "--sandbox-root"
                },
                &root,
            ]);
        }
        command
            .arg("--protocol=jsonl")
            .creation_flags(0x08000000)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let Ok(mut child) = command.spawn() else {
            let _ = tx.send(Event::Error("ENGINE_START_FAILED"));
            let _ = tx.send(Event::End(None));
            return;
        };
        let mut stdin = child.stdin.take().unwrap();
        if writeln!(stdin, "{request}")
            .and_then(|_| stdin.flush())
            .is_err()
        {
            let _ = tx.send(Event::Error("ENGINE_INPUT_FAILED"));
        }
        if let Ok(mut shared) = input.lock() {
            shared.stdin = Some(stdin);
            shared.deliver();
        }
        let mut output = BufReader::new(child.stdout.take().unwrap());
        loop {
            let mut bytes = Vec::new();
            match Read::by_ref(&mut output)
                .take(65537)
                .read_until(b'\n', &mut bytes)
            {
                Ok(0) => break,
                Ok(_) if bytes.len() <= 65536 => match serde_json::from_slice(&bytes) {
                    Ok(value) => {
                        if tx.send(Event::Line(value)).is_err() {
                            break;
                        }
                    }
                    Err(_) => {
                        let _ = tx.send(Event::Error("PROTOCOL_INVALID_EVENT"));
                        break;
                    }
                },
                _ => {
                    let _ = tx.send(Event::Error("PROTOCOL_OUTPUT_LIMIT"));
                    break;
                }
            }
        }
        if let Ok(mut shared) = input.lock() {
            shared.stdin.take();
        } // cooperative disconnect, never hard kill
        drop(output); // an invalid/flooding peer must see pipe closure before wait
        let code = child.wait().ok().and_then(|status| status.code());
        let _ = tx.send(Event::End(code));
    });
}

impl Window {
    fn add(&mut self, id: u16, class: &str, title: &str, style: u32) {
        let control = unsafe {
            CreateWindowExW(
                if class == "EDIT" { 0x200 } else { 0 },
                wide(class).as_ptr(),
                wide(title).as_ptr(),
                0x50000000 | style,
                0,
                0,
                0,
                0,
                self.window,
                id as isize,
                self.instance,
                std::ptr::null_mut(),
            )
        };
        self.controls.insert(id, control);
        if class == "EDIT" {
            unsafe {
                SendMessageW(control, 0xc5, if id == DETAILS { 65536 } else { 32768 }, 0);
            }
        }
    }
    fn layout(&mut self) {
        let dpi = unsafe { GetDpiForWindow(self.window) }.max(96) as i32;
        let scale = |value: i32| value * dpi / 96;
        let font = unsafe {
            CreateFontW(
                -scale(16),
                0,
                0,
                0,
                400,
                0,
                0,
                0,
                1,
                0,
                0,
                5,
                0,
                wide("Segoe UI").as_ptr(),
            )
        };
        for control in self.controls.values() {
            unsafe {
                SendMessageW(*control, 0x30, font as usize, 1);
            }
        }
        if self.font != 0 {
            unsafe {
                DeleteObject(self.font);
            }
        }
        self.font = font;
        let mut rect = Rect::default();
        unsafe {
            GetClientRect(self.window, &mut rect);
        }
        let width = (rect.right * 96 / dpi).max(550);
        let height = (rect.bottom * 96 / dpi).max(590);
        let mut positions = vec![(200, 12, 10, width - 24, 42)];
        for (index, id) in [ROOT, MANIFEST, PAYLOAD, DIGEST].iter().enumerate() {
            positions.push((201 + index as u16, 12, 60 + index as i32 * 34, 148, 26));
            positions.push((*id, 166, 60 + index as i32 * 34, width - 178, 26));
        }
        for (index, id) in [INIT, PLAN, ROLLBACK, STATUS, REPAIR, LICENSES]
            .iter()
            .enumerate()
        {
            positions.push((
                *id,
                12 + index as i32 * (width - 24) / 6,
                204,
                (width - 34) / 6,
                32,
            ));
        }
        positions.extend([
            (SUMMARY, 12, 248, width - 24, 178),
            (DETAILS, 12, 438, width - 24, height - 570),
            (CHECK, 12, height - 120, width - 24, 28),
            (CONFIRM, 12, height - 80, 220, 32),
            (CANCEL, 244, height - 80, 180, 32),
            (PROGRESS, 12, height - 40, width - 24, 30),
        ]);
        for (id, x, y, w, h) in positions {
            unsafe {
                MoveWindow(self.control(id), scale(x), scale(y), scale(w), scale(h), 1);
            }
        }
    }
}

unsafe extern "system" fn procedure(
    window: Handle,
    message: u32,
    wparam: usize,
    lparam: isize,
) -> isize {
    if message == 0x24 && lparam != 0 {
        let dpi = unsafe { GetDpiForWindow(window) }.max(96) as i32;
        let limits = unsafe { &mut *(lparam as *mut MinMax) };
        limits.minimum_track = Point {
            x: 760 * dpi / 96,
            y: 700 * dpi / 96,
        };
        return 0;
    }
    if message == 2 {
        unsafe {
            PostQuitMessage(0);
        }
        return 0;
    }
    let pointer = unsafe { GetWindowLongPtrW(window, -21) } as *const RefCell<Window>;
    if !pointer.is_null() {
        // Native controls synchronously reenter their parent for notifications.
        // Never form overlapping &mut Window references across Win32 calls.
        let Ok(mut state) = (unsafe { &*pointer }).try_borrow_mut() else {
            // DefWindowProc handles WM_CLOSE destructively. Reentrant Close
            // must retain the busy window; ordinary queued Close is handled below.
            if message == 0x10 {
                return 0;
            }
            return unsafe { DefWindowProcW(window, message, wparam, lparam) };
        };
        match message {
            0x111 => {
                state.command(wparam as u16, (wparam >> 16) as u16);
                return 0;
            }
            0x113 => {
                state.poll();
                return 0;
            }
            5 => {
                state.layout();
                return 0;
            }
            0x2e0 => {
                if lparam != 0 {
                    let rect = unsafe { &*(lparam as *const Rect) };
                    unsafe {
                        MoveWindow(
                            window,
                            rect.left,
                            rect.top,
                            rect.right - rect.left,
                            rect.bottom - rect.top,
                            1,
                        );
                    }
                }
                state.layout();
                return 0;
            }
            0x10 => {
                if state.busy {
                    state.cancel();
                    set(
                        state.control(PROGRESS),
                        "Wait for final result before closing / 処理の確定を待って閉じてください",
                    );
                } else {
                    unsafe {
                        DestroyWindow(window);
                    }
                }
                return 0;
            }
            _ => {}
        }
    }
    unsafe { DefWindowProcW(window, message, wparam, lparam) }
}

pub fn show() {
    let arguments: Vec<_> = std::env::args().skip(1).collect();
    let user_mode = !arguments.iter().any(|a| a == "--sandbox-root");
    let instance = unsafe { GetModuleHandleW(std::ptr::null()) };
    let name = wide("VepDevelopmentSetup");
    let class = WindowClass {
        style: 0,
        procedure,
        class_extra: 0,
        window_extra: 0,
        instance,
        icon: 0,
        cursor: unsafe { LoadCursorW(0, 32512usize as *const u16) },
        background: 6,
        menu: std::ptr::null(),
        name: name.as_ptr(),
    };
    if unsafe { RegisterClassW(&class) } == 0 {
        return;
    }
    let window = unsafe {
        CreateWindowExW(
            0x10000,
            name.as_ptr(),
            wide(if user_mode {
                "VEP setup — Windows x64 offline candidate"
            } else {
                "VEP setup — DEVELOPMENT SANDBOX"
            })
            .as_ptr(),
            0xcf0000,
            100,
            80,
            900,
            740,
            0,
            0,
            instance,
            std::ptr::null_mut(),
        )
    };
    if window == 0 {
        return;
    }
    let (tx, rx) = mpsc::sync_channel(16);
    let Ok(executable) = std::env::current_exe() else {
        return;
    };
    let mut state = Box::new(Window {
        window,
        instance,
        font: 0,
        controls: BTreeMap::new(),
        engine: executable.with_file_name("vep-setup.exe"),
        tx,
        rx,
        input: Arc::new(Mutex::new(Pipe::default())),
        counter: 0,
        stream: None,
        operation: String::new(),
        request_inputs: None,
        rollback: false,
        pending: None,
        busy: false,
        compatible: false,
        unknown: false,
        user_mode,
        license_offset: 0,
    });
    state.add(200,"STATIC",if user_mode {"Unsigned local candidate / 未署名のローカル検証候補。昇格せず選択したユーザー領域へ導入します。既存データ・既存ACLは変更しません。"} else {"Development only / 開発用。未署名・供給元未認証。テスト用の空フォルダーだけで使用してください。"},0);
    for (index, (id, label)) in [
        (
            ROOT,
            if user_mode {
                "&Root / 導入先"
            } else {
                "&Root / テスト先"
            },
        ),
        (MANIFEST, "&Manifest"),
        (PAYLOAD, "&Payload"),
        (DIGEST, "&SHA-256"),
    ]
    .iter()
    .enumerate()
    {
        state.add(201 + index as u16, "STATIC", label, 0);
        state.add(*id, "EDIT", "", 0x10080);
    }
    for (id, label) in [
        (INIT, "&Initialize…"),
        (PLAN, "Pre&view install"),
        (ROLLBACK, "Plan roll&back"),
        (STATUS, "S&tatus"),
        (REPAIR, "R&epair…"),
        (LICENSES, "&Licenses >"),
    ] {
        state.add(id, "BUTTON", label, 0x10000);
    }
    state.add(
        SUMMARY,
        "EDIT",
        "Common engine plan / 共通engineの確認内容",
        0x10844,
    );
    state.add(DETAILS, "EDIT", "", 0x210844);
    state.add(
        CHECK,
        "BUTTON",
        "対象・同梱license・未署名候補の条件に同意 / I revie&wed this plan and licenses",
        0x10003,
    );
    state.add(CONFIRM, "BUTTON", "&Confirm / 実行", 0x10001);
    state.add(CANCEL, "BUTTON", "Ca&ncel / 取消", 0x10000);
    state.add(PROGRESS, "STATIC", "Connecting to common engine…", 0);
    for pair in arguments.chunks_exact(2) {
        let id = match pair[0].as_str() {
            "--sandbox-root" | "--user-root" => ROOT,
            "--manifest" => MANIFEST,
            "--payload" => PAYLOAD,
            "--sha256" => DIGEST,
            _ => continue,
        };
        set(state.control(id), &pair[1]);
    }
    state.layout();
    state.buttons(); // no enabled mutation controls before capability handshake
    let state = Box::new(RefCell::new(*state));
    unsafe {
        SetWindowLongPtrW(window, -21, (&*state as *const RefCell<Window>) as isize);
        SetTimer(window, 1, 40, 0);
        ShowWindow(
            window,
            if cfg!(feature = "fault-injection")
                && std::env::var_os("VEP_GUI_TEST_NO_ACTIVATE").is_some()
            {
                4
            } else {
                5
            },
        );
    }
    state.borrow_mut().begin(json!({"op":"capabilities"}));
    let mut message = Message::default();
    while unsafe { GetMessageW(&mut message, 0, 0, 0) } > 0 {
        if unsafe { IsDialogMessageW(window, &mut message) } == 0 {
            unsafe {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
    if state.borrow().font != 0 {
        unsafe {
            DeleteObject(state.borrow().font);
        }
    }
}
