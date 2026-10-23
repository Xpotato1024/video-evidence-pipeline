//! Bounded protocol output is separate from transaction/cancellation authority.
use crate::Result;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, Read, Write},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU8, Ordering},
        mpsc::{self, SyncSender},
    },
};
struct Frame {
    name: &'static str,
    fields: Value,
}
struct Output {
    sender: SyncSender<Frame>,
    writer: Mutex<Option<std::thread::JoinHandle<Result<()>>>>,
    finished: AtomicBool,
    failed: Arc<AtomicBool>,
}
// 0 = cancellable, 1 = requested, 2 = commit boundary crossed.
#[derive(Clone)]
pub struct Operation {
    id: String,
    streaming: bool,
    state: Arc<AtomicU8>,
    output: Option<Arc<Output>>,
}
impl Operation {
    pub fn quiet() -> Self {
        Self {
            id: String::new(),
            streaming: false,
            state: Arc::new(AtomicU8::new(0)),
            output: None,
        }
    }
    pub fn new(id: String, streaming: bool) -> Self {
        let (sender, receiver) = mpsc::sync_channel::<Frame>(32);
        let failed = Arc::new(AtomicBool::new(false));
        let writer_failed = failed.clone();
        let request_id = id.clone();
        let writer = std::thread::spawn(move || {
            let result = (|| {
                let stdout = io::stdout();
                let mut stdout = stdout.lock();
                let mut sequence = 0u64;
                while let Ok(frame) = receiver.recv() {
                    sequence += 1;
                    let mut value = json!({"schema":"vep.setup.v1","id":request_id,"seq":sequence,"event":frame.name});
                    value
                        .as_object_mut()
                        .unwrap()
                        .extend(frame.fields.as_object().unwrap().clone());
                    writeln!(stdout, "{value}")
                        .and_then(|_| stdout.flush())
                        .map_err(|_| "PROTOCOL_OUTPUT_FAILED")?;
                    if frame.name == "result" {
                        return Ok(());
                    }
                }
                Err("PROTOCOL_OUTPUT_FAILED")
            })();
            writer_failed.store(result.is_err(), Ordering::SeqCst);
            result
        });
        Self {
            id,
            streaming,
            state: Arc::new(AtomicU8::new(0)),
            output: Some(Arc::new(Output {
                sender,
                writer: Mutex::new(Some(writer)),
                finished: AtomicBool::new(false),
                failed,
            })),
        }
    }
    pub fn listen(&self) {
        if !self.streaming {
            return;
        }
        let operation = self.clone();
        std::thread::spawn(move || {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Cancel {
                schema: String,
                id: String,
                op: String,
            }
            let mut stdin = io::stdin().lock();
            let mut budget = 1024 * 1024;
            for _ in 0..16 {
                let mut bytes = Vec::new();
                let read = Read::by_ref(&mut stdin)
                    .take(65537)
                    .read_until(b'\n', &mut bytes);
                if matches!(read, Ok(0)) {
                    operation.request_cancel();
                    return;
                }
                if read.is_err() || bytes.len() > 65536 || bytes.len() > budget {
                    return;
                }
                budget -= bytes.len();
                if serde_json::from_slice::<Cancel>(&bytes).is_ok_and(|c| {
                    c.schema == "vep.setup.v1" && c.id == operation.id && c.op == "cancel"
                }) {
                    operation.request_cancel();
                    return;
                }
            }
        });
    }
    fn request_cancel(&self) {
        let accepted = self
            .state
            .compare_exchange(0, 1, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok();
        self.notice("cancel_ack",json!({"accepted":accepted,"final":false,"effect":if accepted {"safe_checkpoint"} else {"commit_deferred"}}));
    }
    // Progress/ack can coalesce under backpressure; neither can block activation.
    fn notice(&self, name: &'static str, fields: Value) {
        if let Some(output) = &self.output {
            if !output.finished.load(Ordering::SeqCst) {
                let _ = output.sender.try_send(Frame { name, fields });
            }
        }
    }
    pub fn check(&self) -> Result<()> {
        if self.state.load(Ordering::SeqCst) == 1 {
            return Err("CANCELLED");
        }
        if self
            .output
            .as_ref()
            .is_some_and(|output| output.failed.load(Ordering::SeqCst))
        {
            return Err("PROTOCOL_OUTPUT_FAILED");
        }
        Ok(())
    }
    pub fn progress(&self, phase: &str, completed: u64, total: u64) -> Result<()> {
        self.check()?;
        if self.streaming {
            self.notice(
                "progress",
                json!({"phase":phase,"completed":completed,"total":total,"cancellable":true}),
            );
        }
        Ok(())
    }
    pub fn commit(&self) -> Result<()> {
        self.state
            .compare_exchange(0, 2, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "CANCELLED")?;
        if self.streaming {
            self.notice("progress", json!({"phase":"ACTIVATE","cancellable":false}));
        }
        #[cfg(feature = "fault-injection")]
        if let Ok(delay) = std::env::var("VEP_SANDBOX_TEST_COMMIT_DELAY_MS") {
            if let Ok(ms) = delay.parse::<u64>() {
                if ms <= 1000 {
                    std::thread::sleep(std::time::Duration::from_millis(ms));
                }
            }
        }
        Ok(())
    }
    // Called only after execute returned and ALL transaction locks were dropped.
    // Final is never coalesced. A blocked consumer can delay exit, not state commit.
    pub fn finish(&self, fields: Value) -> Result<()> {
        let output = self.output.as_ref().ok_or("PROTOCOL_OUTPUT_FAILED")?;
        if output.finished.swap(true, Ordering::SeqCst) {
            return Err("PROTOCOL_ALREADY_FINISHED");
        }
        let sent = output
            .sender
            .send(Frame {
                name: "result",
                fields,
            })
            .map_err(|_| "PROTOCOL_OUTPUT_FAILED");
        let writer = output
            .writer
            .lock()
            .map_err(|_| "PROTOCOL_OUTPUT_FAILED")?
            .take()
            .ok_or("PROTOCOL_ALREADY_FINISHED")?;
        let written = writer.join().map_err(|_| "PROTOCOL_OUTPUT_FAILED")?;
        sent.and(written)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cancellation_and_commit_have_one_winner() {
        for _ in 0..100 {
            let operation = Operation::quiet();
            let other = operation.clone();
            let cancel = std::thread::spawn(move || {
                let _ = other
                    .state
                    .compare_exchange(0, 1, Ordering::SeqCst, Ordering::SeqCst);
            });
            let commit = operation.commit();
            cancel.join().unwrap();
            assert!(matches!(
                (operation.state.load(Ordering::SeqCst), commit),
                (1, Err("CANCELLED")) | (2, Ok(()))
            ));
        }
    }
}
