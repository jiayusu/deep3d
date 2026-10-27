use core_domain::{canonical_bytes, digest_bytes, CoreError, GeneralAcceptance, Result};
use core_storage::HeadToken;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactSource {
    pub path: String,
    pub sha256: String,
    pub head: HeadToken,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TaskSpec {
    pub key: String,
    pub depends_on: Vec<String>,
    pub source_task: Option<String>,
    pub changes_mm: BTreeMap<String, f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acceptance: Option<GeneralAcceptance>,
    pub max_attempts: i32,
    pub timeout_seconds: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RunSpec {
    pub schema_version: String,
    pub run_id: Uuid,
    pub project_id: Uuid,
    pub submission_sha256: String,
    pub source: ArtifactSource,
    pub tasks: Vec<TaskSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubmitRequest {
    pub schema_version: String,
    pub run_id: Uuid,
    pub project: String,
    pub expected_head: HeadToken,
    pub tasks: Vec<TaskSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LeaseToken {
    pub run_id: Uuid,
    pub task_key: String,
    pub attempt_id: Uuid,
    pub worker_id: String,
    pub execution_epoch: i64,
    pub control_epoch: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimRequest {
    pub claim_id: Uuid,
    pub worker_id: String,
    pub run_id: Option<Uuid>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lease {
    pub token: LeaseToken,
    pub source: ArtifactSource,
    pub changes_mm: BTreeMap<String, f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acceptance: Option<GeneralAcceptance>,
    pub lease_until_unix_ms: i64,
    pub deadline_unix_ms: i64,
    pub heartbeat_after_seconds: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TaskOutput {
    pub schema_version: String,
    pub project_id: Uuid,
    /// Coordinator-owned immutable native package, never a worker's mutable directory.
    pub source: ArtifactSource,
    pub materialization: String,
    pub step_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompletionRequest {
    pub lease: LeaseToken,
    pub materialization: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailureRequest {
    pub lease: LeaseToken,
    pub code: String,
    pub retryable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlRequest {
    pub operation_id: Uuid,
    pub expected_control_epoch: i64,
    pub action: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskView {
    pub spec: TaskSpec,
    pub state: String,
    pub execution_epoch: i64,
    pub output: Option<TaskOutput>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunView {
    pub spec: RunSpec,
    pub state: String,
    pub control_epoch: i64,
    pub event_seq: i64,
    pub tasks: Vec<TaskView>,
    pub attempts: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeEvent {
    pub seq: i64,
    pub event_id: Uuid,
    pub kind: String,
    pub payload: serde_json::Value,
}

pub fn valid_parameter_name(name: &str) -> bool {
    !name.trim().is_empty() && name.chars().count() <= 256 && !name.chars().any(char::is_control)
}

pub fn request_digest<T: Serialize>(value: &T) -> Result<String> {
    Ok(digest_bytes(&canonical_bytes(value)?))
}

pub fn valid_hash(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    })
}

pub fn valid_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

pub fn validate_tasks(tasks: &[TaskSpec]) -> Result<()> {
    let invalid = |message| CoreError::new("PLAN_INVALID", message);
    if tasks.is_empty() || tasks.len() > 256 {
        return Err(invalid("A run needs 1 to 256 immutable tasks"));
    }
    let by_key: BTreeMap<_, _> = tasks.iter().map(|task| (task.key.as_str(), task)).collect();
    if by_key.len() != tasks.len() {
        return Err(invalid("Duplicate task key"));
    }
    for task in tasks {
        if !valid_key(&task.key)
            || !(1..=10).contains(&task.max_attempts)
            || !(1..=86400).contains(&task.timeout_seconds)
            || task.changes_mm.is_empty()
            || task.changes_mm.len() > 64
            || task
                .changes_mm
                .iter()
                .any(|(k, v)| !valid_parameter_name(k) || !v.is_finite())
        {
            return Err(invalid(
                "Invalid task identity, limits or parameter changes",
            ));
        }
        if let Some(acceptance) = &task.acceptance {
            acceptance.validate()?;
        }
        let dependencies: BTreeSet<_> = task.depends_on.iter().collect();
        if dependencies.len() != task.depends_on.len()
            || task
                .depends_on
                .iter()
                .any(|key| key == &task.key || !by_key.contains_key(key.as_str()))
            || task
                .source_task
                .as_ref()
                .is_some_and(|key| !dependencies.contains(key))
        {
            return Err(invalid(
                "Dependencies must be unique existing tasks and include source_task",
            ));
        }
    }
    let mut visited = BTreeSet::new();
    loop {
        let before = visited.len();
        for task in tasks {
            if task.depends_on.iter().all(|key| visited.contains(key)) {
                visited.insert(task.key.clone());
            }
        }
        if visited.len() == tasks.len() {
            return Ok(());
        }
        if before == visited.len() {
            return Err(invalid("Task graph must be acyclic"));
        }
    }
}

pub fn validate_spec(spec: &RunSpec) -> Result<()> {
    if spec.schema_version != "deep3d.durable-run/1"
        || spec.run_id.is_nil()
        || spec.project_id.is_nil()
        || !valid_hash(&spec.source.sha256)
        || !valid_hash(&spec.submission_sha256)
        || spec.source.path.is_empty()
        || spec.source.path.len() > 512
    {
        return Err(CoreError::new(
            "RUN_INVALID",
            "Invalid frozen run definition",
        ));
    }
    validate_tasks(&spec.tasks)
}
