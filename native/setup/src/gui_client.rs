//! Protocol-only GUI state. This module never copies files or activates releases.
use serde_json::{Value, json};

#[derive(Clone, PartialEq, Eq)]
pub struct Inputs {
    pub root: String,
    pub manifest: String,
    pub payload: String,
    pub digest: String,
}
impl Inputs {
    pub fn fields(&self) -> Value {
        json!({"manifest":self.manifest,"payload":self.payload,"manifest_sha256":self.digest})
    }
}
pub struct ConfirmedPlan {
    pub value: Value,
    pub inputs: Inputs,
    pub rollback: bool,
}
impl ConfirmedPlan {
    pub fn new(value: Value, inputs: Inputs, rollback: bool) -> Result<Self, &'static str> {
        let body = &value["plan"];
        if value["development_only"] != true
            || value["publisher_authenticated"] != false
            || body["schema"] != "vep.sandbox-plan.v1"
            || body["root"] != inputs.root
            || body["manifest_sha256"] != inputs.digest
            || !body["old_generation"].is_u64()
            || !body["release_id"].is_string()
            || !value["plan_id"].is_string()
            || !value["expanded_bytes"].is_u64()
        {
            return Err("PROTOCOL_INVALID_PLAN");
        }
        Ok(Self {
            value,
            inputs,
            rollback,
        })
    }
    pub fn request(&self, current: &Inputs) -> Result<Value, &'static str> {
        if current != &self.inputs {
            return Err("PLAN_STALE");
        }
        let mut request = if self.rollback {
            json!({"op":"rollback","release_id":self.value["plan"]["release_id"]})
        } else {
            let mut fields = self.inputs.fields();
            fields["op"] = json!("apply");
            fields
        };
        request["plan_id"] = self.value["plan_id"].clone();
        Ok(request)
    }
}
pub struct Stream {
    pub id: String,
    pub sequence: u64,
    pub final_seen: bool,
    pub status: Option<String>,
}
impl Stream {
    pub fn exit_matches(&self, code: Option<i32>) -> bool {
        matches!(
            (self.status.as_deref(), code),
            (Some("ok"), Some(0)) | (Some("failed"), Some(1 | 2)) | (Some("cancelled"), Some(3))
        )
    }
    pub fn accept(&mut self, value: &Value) -> Result<(), &'static str> {
        let sequence = value["seq"].as_u64().ok_or("PROTOCOL_INVALID_EVENT")?;
        if self.final_seen
            || value["schema"] != "vep.setup.v1"
            || value["id"] != self.id
            || sequence != self.sequence + 1
        {
            return Err("PROTOCOL_INVALID_EVENT");
        }
        match value["event"].as_str() {
            Some("result") => {
                if !matches!(
                    value["status"].as_str(),
                    Some("ok" | "failed" | "cancelled")
                ) {
                    return Err("PROTOCOL_INVALID_EVENT");
                }
                self.final_seen = true;
                self.status = value["status"].as_str().map(str::to_string);
            }
            Some("progress" | "cancel_ack") => {}
            _ => return Err("PROTOCOL_INVALID_EVENT"),
        }
        self.sequence = sequence;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn inputs() -> Inputs {
        Inputs {
            root: "test-root".into(),
            manifest: "manifest".into(),
            payload: "payload".into(),
            digest: "hash".into(),
        }
    }
    #[test]
    fn confirmation_uses_engine_plan_and_input_change_invalidates_it() {
        let fields = inputs();
        let value = json!({"development_only":true,"publisher_authenticated":false,"expanded_bytes":10,"plan_id":"engine-id","plan":{"schema":"vep.sandbox-plan.v1","root":"test-root","manifest_sha256":"hash","release_id":"r1","old_generation":3}});
        let plan = ConfirmedPlan::new(value.clone(), fields.clone(), false).unwrap();
        assert_eq!(plan.request(&fields).unwrap()["plan_id"], "engine-id");
        let mut changed = fields.clone();
        changed.payload = "other".into();
        assert_eq!(plan.request(&changed).unwrap_err(), "PLAN_STALE");
        let rollback = ConfirmedPlan::new(value, fields.clone(), true).unwrap();
        assert_eq!(
            rollback.request(&fields).unwrap(),
            json!({"op":"rollback","release_id":"r1","plan_id":"engine-id"})
        );
    }
    #[test]
    fn mismatched_id_sequence_duplicate_final_and_unknown_event_fail_closed() {
        let mut stream = Stream {
            id: "ui".into(),
            sequence: 0,
            final_seen: false,
            status: None,
        };
        assert!(
            stream
                .accept(&json!({"schema":"vep.setup.v1","id":"other","seq":1,"event":"progress"}))
                .is_err()
        );
        assert!(
            stream
                .accept(&json!({"schema":"vep.setup.v1","id":"ui","seq":2,"event":"progress"}))
                .is_err()
        );
        stream
            .accept(
                &json!({"schema":"vep.setup.v1","id":"ui","seq":1,"event":"result","status":"ok"}),
            )
            .unwrap();
        assert!(
            stream
                .accept(&json!({"schema":"vep.setup.v1","id":"ui","seq":2,"event":"progress"}))
                .is_err()
        );
    }
}
