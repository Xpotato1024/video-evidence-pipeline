//! Bounded synthetic malformed/flooding peer for the GUI's pipe-error tests.
use std::io::{self, BufRead, Write};
fn main() {
    let mut line = String::new();
    io::stdin().lock().read_line(&mut line).unwrap();
    let id = line
        .split("\"id\":\"")
        .nth(1)
        .unwrap()
        .split('"')
        .next()
        .unwrap();
    if line.contains("\"op\":\"capabilities\"") {
        println!(
            "{{\"schema\":\"vep.setup.v1\",\"id\":\"{id}\",\"seq\":1,\"event\":\"result\",\"status\":\"ok\",\"value\":{{\"streaming_events\":true,\"development_only\":true}}}}"
        );
    } else if line.contains("\"op\":\"status\"") {
        println!(
            "{{\"schema\":\"vep.setup.v1\",\"id\":\"{id}\",\"seq\":1,\"event\":\"result\",\"status\":\"ok\",\"value\":{{\"fixture_peer\":true,\"recovery_required\":false}}}}"
        );
    } else {
        let args: Vec<_> = std::env::args().collect();
        let mut stdout = io::stdout().lock();
        let first = if args.iter().any(|arg| arg.ends_with("oversized")) {
            "x".repeat(65537)
        } else {
            "{invalid-json".into()
        };
        if writeln!(stdout, "{first}")
            .and_then(|_| stdout.flush())
            .is_err()
        {
            return;
        }
        // More than a pipe buffer, bounded at 2 MiB. Reader must close before wait.
        let block = "x".repeat(4096);
        for _ in 0..512 {
            if writeln!(stdout, "{block}").is_err() {
                return;
            }
        }
    }
}
