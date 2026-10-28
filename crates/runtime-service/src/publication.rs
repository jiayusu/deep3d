//! The coordinator reopens actual geometry before accepting a public commit.
//!
//! Worker reports are useful diagnostics but cannot authorize publication. The
//! fresh verifier receives the independently frozen Goal and exact package bytes.

use crate::artifacts;
use core_domain::{canonical_bytes, digest_bytes, Acceptance, CoreError, Result};
use core_runtime::{valid_hash, GoalRevision, PublicationEvidence, TaskOutput};
use core_storage::{Materialization, ProjectStore};
use serde_json::Value;
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};
use wait_timeout::ChildExt;

fn io(cause: impl std::fmt::Display) -> CoreError {
    CoreError::new("PUBLICATION_IO", cause.to_string())
}

pub fn python() -> PathBuf {
    if let Some(path) = std::env::var_os("DEEP3D_VERIFIER_PYTHON") {
        return path.into();
    }
    for path in [".venv/Scripts/python.exe", ".venv/bin/python"] {
        if Path::new(path).is_file() {
            return std::path::absolute(path).unwrap_or_else(|_| PathBuf::from(path));
        }
    }
    PathBuf::from("python3")
}

fn report_bytes(path: &Path) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    File::open(path)
        .map_err(io)?
        .take(8 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(io)?;
    if bytes.is_empty() || bytes.len() > 8 * 1024 * 1024 {
        return Err(CoreError::new("EVIDENCE_INVALID", "Verifier report is not bounded"));
    }
    Ok(bytes)
}

fn persist_report(root: &Path, bytes: &[u8]) -> Result<String> {
    let digest = digest_bytes(bytes);
    let objects = root.join("objects");
    fs::create_dir_all(&objects).map_err(io)?;
    let target = objects.join(format!("{}.json", &digest[7..]));
    let mut temp = tempfile::NamedTempFile::new_in(&objects).map_err(io)?;
    temp.write_all(bytes).map_err(io)?;
    temp.as_file().sync_all().map_err(io)?;
    if let Err(cause) = temp.persist_noclobber(&target) {
        if cause.error.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(io(cause.error));
        }
        if report_bytes(&target)? != bytes {
            return Err(CoreError::new("ARTIFACT_CORRUPT", "Evidence object identity collision"));
        }
    }
    #[cfg(unix)]
    File::open(&objects).and_then(|file| file.sync_all()).map_err(io)?;
    Ok(digest)
}

pub fn read_report(root: &Path, digest: &str) -> Result<Value> {
    if !valid_hash(digest) {
        return Err(CoreError::new("EVIDENCE_INVALID", "Invalid evidence digest"));
    }
    let path = artifacts::relative_file(root, &format!("objects/{}.json", &digest[7..]))?;
    let bytes = report_bytes(&path)?;
    if digest_bytes(&bytes) != digest {
        return Err(CoreError::new("ARTIFACT_CORRUPT", "Public evidence object changed"));
    }
    serde_json::from_slice(&bytes).map_err(|_| CoreError::new("EVIDENCE_INVALID", "Invalid evidence JSON"))
}

fn copy_artifact(store: &ProjectStore, record: &Materialization, name: &str, out: &Path) -> Result<()> {
    let reference = record.files.get(name).ok_or_else(|| {
        CoreError::new("EVIDENCE_INVALID", format!("Missing verifier input {name}"))
    })?;
    let bytes = store.read_blob(&reference.sha256)?;
    if digest_bytes(&bytes) != reference.sha256 || bytes.len() as u64 != reference.size_bytes {
        return Err(CoreError::new("ARTIFACT_CORRUPT", "Verifier input changed"));
    }
    let target = out.join(name);
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(io)?;
    }
    fs::write(target, bytes).map_err(io)
}

fn run_verifier(
    python: &Path,
    directory: &Path,
    mechanical: bool,
    stage: &str,
    record: &Materialization,
    goal: &GoalRevision,
) -> Result<Vec<u8>> {
    let report = directory.join(format!("authority-{stage}.json"));
    let log = File::create(directory.join(format!("authority-{stage}.log"))).map_err(io)?;
    let mut command = Command::new(python);
    command.arg("-m");
    if mechanical {
        command.args(["deep3d_cad.mechanical_verifier", "--directory"])
            .arg(directory).args(["--stage", stage]);
    } else {
        command.arg("deep3d_cad.worker")
            .arg(if stage == "native" {"verify-brep"} else {"verify-step"})
            .arg("--spec").arg(directory.join("goal.json"))
            .arg("--precision").arg(directory.join("precision.json"))
            .arg("--shape").arg(directory.join(if stage == "native" {"flange.brep"} else {"flange.step"}));
    }
    command.arg("--report").arg(&report)
        .stdin(Stdio::null()).stdout(log.try_clone().map_err(io)?).stderr(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().map_err(|_| {
        CoreError::new("VERIFIER_UNAVAILABLE", "Configure DEEP3D_VERIFIER_PYTHON with the locked CAD environment")
    })?;
    let status = child.wait_timeout(Duration::from_secs(120)).map_err(io)?;
    let Some(status) = status else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(CoreError::new("VERIFIER_TIMEOUT", "Public geometry verification exceeded its deadline"));
    };
    if !status.success() {
        return Err(CoreError::new("PUBLICATION_REJECTED", "Fresh coordinator geometry verification did not pass"));
    }
    let bytes = report_bytes(&report)?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| CoreError::new("EVIDENCE_INVALID", "Verifier returned invalid JSON"))?;
    let geometry = match (mechanical, stage) {
        (true, "native") => "model.brep",
        (true, _) => "model.step",
        (false, "native") => "flange.brep",
        (false, _) => "flange.step",
    };
    let checks = value["checks"].as_array().ok_or_else(|| {
        CoreError::new("EVIDENCE_INVALID", "Verifier returned no check results")
    })?;
    let expected_schema = if mechanical {"deep3d.mechanical-verification/1"} else {"deep3d.flange-verification/1"};
    if value["status"] != "pass" || value["stage"] != stage
        || value["schema_version"] != expected_schema
        || value["input_artifact_sha256"] != record.files[geometry].sha256
        || canonical_bytes(&value["expected_spec"])? != canonical_bytes(&goal.definition.acceptance)?
        || canonical_bytes(&value["precision_profile"])? != canonical_bytes(&goal.definition.precision)?
        || checks.is_empty() || checks.iter().any(|c| c["status"] != "pass")
        || !value["validator_source_sha256"].as_str().is_some_and(valid_hash)
    {
        return Err(CoreError::new("EVIDENCE_INVALID", "Fresh evidence is not bound to this Goal and geometry"));
    }
    if mechanical && stage == "native"
        && (canonical_bytes(&value["input_node_artifact_sha256"])? != canonical_bytes(&record.node_outputs)?
            || value["input_assembly_artifact_sha256"] != record.files["assembly.json"].sha256)
    {
        return Err(CoreError::new("EVIDENCE_INVALID", "Fresh node or assembly evidence is unbound"));
    }
    Ok(bytes)
}

pub fn verify(
    root: &Path,
    python: &Path,
    goal: &GoalRevision,
    output: &TaskOutput,
) -> Result<PublicationEvidence> {
    let source = artifacts::relative_file(root, &output.source.path)?;
    let bytes = artifacts::read_package(&source)?;
    if digest_bytes(&bytes) != output.source.sha256 {
        return Err(CoreError::new("ARTIFACT_CORRUPT", "Candidate source no longer matches its identity"));
    }
    let file = artifacts::temporary(root, &bytes)?;
    let store = ProjectStore::open(file.path())?;
    let snapshot = store.head()?;
    if snapshot.project.project_id != goal.project_id || snapshot.head != output.source.head {
        return Err(CoreError::new("PUBLICATION_REJECTED", "Candidate project or revision differs"));
    }
    let plan = snapshot.project.compile(snapshot.revision.id)?;
    if canonical_bytes(&plan.acceptance)? != canonical_bytes(&goal.definition.acceptance)?
        || canonical_bytes(&plan.precision)? != canonical_bytes(&goal.definition.precision)?
    {
        return Err(CoreError::new("GOAL_MISMATCH", "Candidate does not implement the frozen independent Goal contract"));
    }
    let record = store.materializations(snapshot.revision.id)?.into_iter()
        .find(|(hash, _)| hash == &output.materialization).map(|(_, record)| record)
        .ok_or_else(|| CoreError::new("EVIDENCE_INVALID", "Candidate has no bound materialization"))?;
    let mechanical = matches!(plan.acceptance, Acceptance::Mechanical(_));
    let verification = root.join("verification");
    fs::create_dir_all(&verification).map_err(io)?;
    let directory = tempfile::tempdir_in(verification).map_err(io)?;
    for name in ["plan.json", "goal.json", "precision.json"] {
        copy_artifact(&store, &record, name, directory.path())?;
    }
    let (native_name, step_name) = if mechanical {
        copy_artifact(&store, &record, "assembly.json", directory.path())?;
        for node in plan.outputs.keys() {
            copy_artifact(&store, &record, &format!("nodes/{node}.brep"), directory.path())?;
        }
        ("model.brep", "model.step")
    } else {
        ("flange.brep", "flange.step")
    };
    for name in [native_name, step_name] {
        copy_artifact(&store, &record, name, directory.path())?;
    }
    let native = run_verifier(python, directory.path(), mechanical, "native", &record, goal)?;
    let step = run_verifier(python, directory.path(), mechanical, "step", &record, goal)?;
    if record.files[step_name].sha256 != output.step_sha256 {
        return Err(CoreError::new("EVIDENCE_INVALID", "STEP identity differs from the accepted candidate"));
    }
    Ok(PublicationEvidence {
        schema_version: "deep3d.publication-evidence/1".into(),
        goal_revision_id: goal.revision_id,
        acceptance_sha256: digest_bytes(&canonical_bytes(&goal.definition.acceptance)?),
        precision_sha256: digest_bytes(&canonical_bytes(&goal.definition.precision)?),
        source_sha256: output.source.sha256.clone(),
        materialization: output.materialization.clone(),
        native_report_sha256: persist_report(root, &native)?,
        step_report_sha256: persist_report(root, &step)?,
        native_geometry_sha256: record.files[native_name].sha256.clone(),
        step_geometry_sha256: record.files[step_name].sha256.clone(),
        assembly_sha256: record.files.get("assembly.json").map(|f| f.sha256.clone()),
    })
}
