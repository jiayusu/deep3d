//! A frozen spec is evaluated outside the project write lock, then checked and attached.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use core_domain::{canonical_bytes, digest_bytes, CoreError, ExecutionPlan, Result};
use core_storage::{ArtifactRef, Materialization, ProjectStore, Snapshot};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;
use wait_timeout::ChildExt;

const MAX_BLOB: u64 = 64 * 1024 * 1024;
const MAX_TOTAL: u64 = 256 * 1024 * 1024;

#[derive(Deserialize)]
struct WorkerArtifact {
    path: String,
    sha256: String,
    size_bytes: u64,
}

#[derive(Deserialize)]
struct WorkerResult {
    schema_version: String,
    status: String,
    project_id: Uuid,
    revision_id: Uuid,
    environment: BTreeMap<String, String>,
    evaluator_source_sha256: String,
    feature_outputs: BTreeMap<Uuid, String>,
    node_outputs: BTreeMap<Uuid, String>,
    artifacts: Vec<WorkerArtifact>,
}

fn io_error(error: impl std::fmt::Display) -> CoreError {
    CoreError::new("IO_ERROR", error.to_string())
}

fn python_path(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return interpreter_path(path);
    }
    for candidate in [".venv/Scripts/python.exe", ".venv/bin/python"] {
        let path = Path::new(candidate);
        if path.is_file() {
            return interpreter_path(path);
        }
    }
    Err(CoreError::new(
        "PYTHON_NOT_CONFIGURED",
        "Run uv sync --frozen, or pass --python explicitly",
    ))
}

// Absolute but not canonical: resolving a virtual environment's interpreter symlink
// would run the base interpreter without the environment's packages.
fn interpreter_path(path: &Path) -> Result<PathBuf> {
    if !path.is_file() {
        return Err(CoreError::new("PYTHON_NOT_CONFIGURED", "Python interpreter not found"));
    }
    std::path::absolute(path).map_err(io_error)
}

fn file_bytes(path: &Path, maximum: u64) -> Result<Vec<u8>> {
    let metadata = path.symlink_metadata().map_err(io_error)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > maximum {
        return Err(CoreError::new(
            "ARTIFACT_INVALID",
            "Expected a bounded ordinary file",
        ));
    }
    let mut bytes = Vec::new();
    File::open(path)
        .map_err(io_error)?
        .take(maximum + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() as u64 > maximum {
        return Err(CoreError::new(
            "ARTIFACT_TOO_LARGE",
            "Artifact exceeds its limit",
        ));
    }
    Ok(bytes)
}

fn ingest(
    directory: &Path,
    result: WorkerResult,
    plan: &ExecutionPlan,
) -> Result<(Materialization, BTreeMap<String, Vec<u8>>)> {
    if result.schema_version != "deep3d.project-evaluation/1"
        || result.status != "pass"
        || result.project_id != plan.project_id
        || result.revision_id != plan.revision_id
    {
        return Err(CoreError::new(
            "EVALUATION_REJECTED",
            "Worker result is not bound to the requested plan",
        ));
    }
    if result
        .environment
        .get("cadquery_ocp_novtk")
        .map(String::as_str)
        != Some("8.0.1.0.0")
    {
        return Err(CoreError::new(
            "ENGINE_UNSUPPORTED",
            "Unexpected OCCT binding build",
        ));
    }
    let expected_features: BTreeSet<_> = plan.features.iter().map(|f| f.id).collect();
    if result
        .feature_outputs
        .keys()
        .copied()
        .collect::<BTreeSet<_>>()
        != expected_features
    {
        return Err(CoreError::new(
            "ARTIFACT_INVALID",
            "Feature output closure differs from the plan",
        ));
    }
    let expected_nodes: BTreeMap<_, _> = plan
        .outputs
        .iter()
        .map(|(node, feature)| (*node, result.feature_outputs[feature].clone()))
        .collect();
    if result.node_outputs != expected_nodes {
        return Err(CoreError::new(
            "ARTIFACT_INVALID",
            "Scene bindings differ from the evaluated feature outputs",
        ));
    }
    let canonical_root = directory.canonicalize().map_err(io_error)?;
    let mut blobs = BTreeMap::new();
    let mut files = BTreeMap::new();
    let mut total = 0u64;
    for artifact in result.artifacts {
        let relative = Path::new(&artifact.path);
        if artifact.path.contains('\\')
            || relative
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
            || relative.as_os_str().is_empty()
        {
            return Err(CoreError::new(
                "ARTIFACT_INVALID",
                "Artifact path must be a relative POSIX path",
            ));
        }
        let path = directory.join(relative);
        let resolved = path.canonicalize().map_err(io_error)?;
        if !resolved.starts_with(&canonical_root) {
            return Err(CoreError::new(
                "ARTIFACT_INVALID",
                "Artifact escapes the worker output directory",
            ));
        }
        let bytes = file_bytes(&path, MAX_BLOB)?;
        if bytes.len() as u64 != artifact.size_bytes || digest_bytes(&bytes) != artifact.sha256 {
            return Err(CoreError::new(
                "ARTIFACT_CORRUPT",
                "Worker artifact hash or size does not match",
            ));
        }
        total = total
            .checked_add(bytes.len() as u64)
            .ok_or_else(|| CoreError::new("ARTIFACT_TOO_LARGE", "Artifact size overflow"))?;
        if total > MAX_TOTAL {
            return Err(CoreError::new(
                "ARTIFACT_TOO_LARGE",
                "Total artifact size exceeds 256 MiB",
            ));
        }
        if files
            .insert(
                artifact.path,
                ArtifactRef {
                    sha256: artifact.sha256.clone(),
                    size_bytes: artifact.size_bytes,
                },
            )
            .is_some()
        {
            return Err(CoreError::new(
                "ARTIFACT_INVALID",
                "Duplicate artifact name",
            ));
        }
        blobs.insert(artifact.sha256, bytes);
    }
    let plan_bytes = canonical_bytes(plan)?;
    let plan_hash = digest_bytes(&plan_bytes);
    blobs.insert(plan_hash.clone(), plan_bytes);
    let mut engine = result.environment;
    engine.insert(
        "evaluator_source_sha256".into(),
        result.evaluator_source_sha256,
    );
    Ok((
        Materialization {
            schema_version: "deep3d.materialization/1".into(),
            project_id: plan.project_id,
            revision_id: plan.revision_id,
            plan_hash,
            engine,
            files,
            feature_outputs: result.feature_outputs,
            node_outputs: result.node_outputs,
        },
        blobs,
    ))
}

pub fn evaluate(
    store: &ProjectStore,
    snapshot: &Snapshot,
    python: Option<&Path>,
    output: &Path,
    timeout: u64,
    cache_dir: Option<&Path>,
    force_rebuild: bool,
) -> Result<Value> {
    if output.exists() {
        return Err(CoreError::new(
            "OUTPUT_EXISTS",
            "Use a new output directory; existing files are preserved",
        ));
    }
    let plan = snapshot.project.compile(snapshot.revision.id)?;
    let directory = tempfile::tempdir().map_err(io_error)?;
    let request = directory.path().join("plan.json");
    std::fs::write(&request, canonical_bytes(&plan)?).map_err(io_error)?;
    let output = std::path::absolute(output).map_err(io_error)?;
    let log_path = directory.path().join("worker.log");
    let log = File::create(&log_path).map_err(io_error)?;
    let mut command = Command::new(python_path(python)?);
    command
        .arg("-m")
        .arg("deep3d_cad.project_worker")
        .arg("--plan")
        .arg(&request)
        .arg("--out")
        .arg(&output)
        .arg("--timeout")
        .arg(timeout.to_string())
        .stdin(Stdio::null())
        .stdout(log.try_clone().map_err(io_error)?)
        .stderr(log);
    if let Some(cache) = cache_dir {
        command.arg("--cache-dir").arg(std::path::absolute(cache).map_err(io_error)?);
    }
    if force_rebuild {
        command.arg("--force-rebuild");
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().map_err(io_error)?;
    let status = child
        .wait_timeout(Duration::from_secs(timeout + 10))
        .map_err(io_error)?;
    let Some(status) = status else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(CoreError::new(
            "WORKER_TIMEOUT",
            "Project evaluation exceeded its deadline; project was not modified",
        ));
    };
    let result_path = output.join("result.json");
    if !status.success() {
        let detail = if result_path.is_file() {
            String::from_utf8_lossy(&file_bytes(&result_path, 8 * 1024 * 1024)?)
                .chars()
                .take(4000)
                .collect::<String>()
        } else {
            String::from_utf8_lossy(&file_bytes(&log_path, 8 * 1024 * 1024)?)
                .chars()
                .take(4000)
                .collect::<String>()
        };
        return Err(CoreError::new(
            "EVALUATION_FAILED",
            format!("Project unchanged; worker rejected evaluation: {detail}"),
        ));
    }
    let result: WorkerResult = serde_json::from_slice(&file_bytes(&result_path, 8 * 1024 * 1024)?)
        .map_err(|e| CoreError::new("WORKER_PROTOCOL_ERROR", e.to_string()))?;
    let (record, blobs) = ingest(&output, result, &plan)?;
    let materialization = store.attach_materialization(record, blobs)?;
    Ok(
        json!({"project_id": plan.project_id, "revision_id": plan.revision_id, "materialization": materialization, "head": store.head()?.head, "output": output}),
    )
}
