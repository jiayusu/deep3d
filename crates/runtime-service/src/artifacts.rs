use core_domain::{canonical_bytes, digest_bytes, CoreError, Result, ScalarValue};
use core_runtime::{ArtifactSource, LeaseToken, SubmitRequest, TaskOutput, TaskSpec};
use core_storage::{ProjectStore, MAX_ARCHIVE_BYTES};
use std::{
    fs,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};
use tempfile::NamedTempFile;

fn io(error: impl std::fmt::Display) -> CoreError {
    CoreError::new("ARTIFACT_IO", error.to_string())
}

pub fn relative_file(root: &Path, name: &str) -> Result<PathBuf> {
    let path = Path::new(name);
    if name.len() > 512
        || name.contains('\\')
        || name.contains(':')
        || path.is_absolute()
        || !path
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
    {
        return Err(CoreError::new(
            "ARTIFACT_PATH",
            "Expected a relative artifact path",
        ));
    }
    let absolute = root.join(path).canonicalize().map_err(io)?;
    if !absolute.starts_with(root) || !absolute.is_file() {
        return Err(CoreError::new(
            "ARTIFACT_PATH",
            "Artifact must be a file inside the runtime root",
        ));
    }
    Ok(absolute)
}

fn read_package(path: &Path) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(io)?
        .take(MAX_ARCHIVE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(io)?;
    if bytes.len() as u64 > MAX_ARCHIVE_BYTES {
        return Err(CoreError::new(
            "ARTIFACT_TOO_LARGE",
            "Native package exceeds 256 MiB",
        ));
    }
    Ok(bytes)
}

fn temporary(root: &Path, bytes: &[u8]) -> Result<NamedTempFile> {
    let objects = root.join("objects");
    fs::create_dir_all(&objects).map_err(io)?;
    let mut file = NamedTempFile::new_in(objects).map_err(io)?;
    file.write_all(bytes).map_err(io)?;
    file.as_file().sync_all().map_err(io)?;
    Ok(file)
}

fn persist(
    root: &Path,
    file: NamedTempFile,
    bytes: &[u8],
    head: core_storage::HeadToken,
) -> Result<ArtifactSource> {
    let sha256 = digest_bytes(bytes);
    let path = format!("objects/{}.deep3d", &sha256[7..]);
    let target = root.join(&path);
    if let Err(error) = file.persist_noclobber(&target) {
        if error.error.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(io(error.error));
        }
        if read_package(&target)? != bytes {
            return Err(CoreError::new(
                "ARTIFACT_CORRUPT",
                "Immutable artifact identity collision",
            ));
        }
    }
    #[cfg(unix)]
    fs::File::open(root.join("objects"))
        .and_then(|file| file.sync_all())
        .map_err(io)?;
    Ok(ArtifactSource { path, sha256, head })
}

pub fn import_native(root: &Path, bytes: &[u8]) -> Result<(uuid::Uuid, ArtifactSource)> {
    if bytes.len() as u64 > MAX_ARCHIVE_BYTES {
        return Err(CoreError::new(
            "ARTIFACT_TOO_LARGE",
            "Native package exceeds 256 MiB",
        ));
    }
    let file = temporary(root, bytes)?;
    let snapshot = ProjectStore::open(file.path())?.head()?;
    let source = persist(root, file, bytes, snapshot.head)?;
    Ok((snapshot.project.project_id, source))
}

pub fn freeze(root: &Path, request: &SubmitRequest) -> Result<(uuid::Uuid, ArtifactSource)> {
    let path = relative_file(root, &request.project)?;
    // Read one file handle, validate the copy and its requested token, then persist
    // the exact bytes. A concurrent source replacement cannot mix two versions.
    let bytes = read_package(&path)?;
    let file = temporary(root, &bytes)?;
    let snapshot = ProjectStore::open(file.path())?.head()?;
    if snapshot.head != request.expected_head {
        return Err(CoreError::new(
            "REVISION_CONFLICT",
            "Source changed before the run was frozen",
        ));
    }
    let source = persist(root, file, &bytes, snapshot.head)?;
    Ok((snapshot.project.project_id, source))
}

pub fn candidate_path(token: &LeaseToken) -> String {
    format!(
        "runs/{}/tasks/{}/attempts/{}/candidate.deep3d",
        token.run_id, token.task_key, token.attempt_id
    )
}

pub fn verify_result(
    root: &Path,
    token: &LeaseToken,
    source: &ArtifactSource,
    task: &TaskSpec,
    materialization: &str,
) -> Result<TaskOutput> {
    let source_path = relative_file(root, &source.path)?;
    let original_bytes = read_package(&source_path)?;
    if digest_bytes(&original_bytes) != source.sha256 {
        return Err(CoreError::new(
            "ARTIFACT_CORRUPT",
            "Frozen input bytes changed",
        ));
    }
    let original_file = temporary(root, &original_bytes)?;
    let original = ProjectStore::open(original_file.path())?;
    let before = original.head()?;
    if before.head != source.head {
        return Err(CoreError::new(
            "ARTIFACT_CORRUPT",
            "Frozen input version differs",
        ));
    }
    let candidate = relative_file(root, &candidate_path(token))?;
    let bytes = read_package(&candidate)?;
    let file = temporary(root, &bytes)?;
    let store = ProjectStore::open(file.path())?;
    let snapshot = store.head()?;
    let mut expected = before.project.clone();
    for (name, value) in &task.changes_mm {
        let id = expected.parameter_id(name)?;
        let parameter = expected
            .parameters
            .get_mut(&id)
            .ok_or_else(|| CoreError::new("PARAMETER_INVALID", "Missing task parameter"))?;
        if !matches!(parameter.value, ScalarValue::LengthMm(_)) {
            return Err(CoreError::new(
                "TYPE_MISMATCH",
                "Runtime changes_mm only edits length parameters",
            ));
        }
        parameter.value = ScalarValue::LengthMm(*value);
    }
    if let Some(acceptance) = &task.acceptance {
        if expected.flange.is_some() {
            return Err(CoreError::new(
                "TYPE_MISMATCH",
                "Legacy flange acceptance follows its parameter binding",
            ));
        }
        expected.acceptance = Some(acceptance.clone());
    }
    expected.validate()?;
    let history = store.history()?;
    let source_history = original.history()?;
    if canonical_bytes(&snapshot.project)? != canonical_bytes(&expected)?
        || snapshot.revision.parent != Some(source.head.revision_id)
        || snapshot.revision.transaction_id != Some(token.attempt_id)
        || source.head.generation.checked_add(1) != Some(snapshot.head.generation)
        || history.len() != source_history.len() + 1
        || history[..source_history.len()] != source_history
    {
        return Err(CoreError::new(
            "CANDIDATE_MISMATCH",
            "Candidate differs from the frozen task edit",
        ));
    }
    let record = store
        .materializations(snapshot.revision.id)?
        .into_iter()
        .find(|(hash, _)| hash == materialization)
        .map(|(_, value)| value)
        .ok_or_else(|| {
            CoreError::new(
                "MATERIALIZATION_NOT_FOUND",
                "No verified materialization for this exact task revision",
            )
        })?;
    let step_name = if snapshot.project.flange.is_some() {
        "flange.step"
    } else {
        "model.step"
    };
    let step = record
        .files
        .get(step_name)
        .ok_or_else(|| CoreError::new("ARTIFACT_MISSING", "Verified STEP is missing"))?;
    let project_id = snapshot.project.project_id;
    let persisted = persist(root, file, &bytes, snapshot.head)?;
    Ok(TaskOutput {
        schema_version: "deep3d.task-output/1".into(),
        project_id,
        source: persisted,
        materialization: materialization.into(),
        step_sha256: step.sha256.clone(),
    })
}

pub fn download(
    root: &Path,
    output: &TaskOutput,
    kind: &str,
) -> Result<(Vec<u8>, String, uuid::Uuid)> {
    let path = relative_file(root, &output.source.path)?;
    let bytes = read_package(&path)?;
    if digest_bytes(&bytes) != output.source.sha256 {
        return Err(CoreError::new(
            "ARTIFACT_CORRUPT",
            "Accepted native package digest differs",
        ));
    }
    if kind == "native" {
        return Ok((
            bytes,
            output.source.sha256.clone(),
            output.source.head.revision_id,
        ));
    }
    let file = temporary(root, &bytes)?;
    let store = ProjectStore::open(file.path())?;
    let snapshot = store.read_revision(output.source.head.revision_id)?;
    let record = store
        .materializations(output.source.head.revision_id)?
        .into_iter()
        .find(|(digest, _)| digest == &output.materialization)
        .map(|(_, record)| record)
        .ok_or_else(|| CoreError::new("ARTIFACT_CORRUPT", "Accepted materialization is missing"))?;
    let step_name = if snapshot.project.flange.is_some() {
        "flange.step"
    } else {
        "model.step"
    };
    let artifact = record
        .files
        .get(step_name)
        .ok_or_else(|| CoreError::new("ARTIFACT_CORRUPT", "Accepted STEP is missing"))?;
    if artifact.sha256 != output.step_sha256 {
        return Err(CoreError::new(
            "ARTIFACT_CORRUPT",
            "Accepted STEP digest differs",
        ));
    }
    Ok((
        store.read_blob(&artifact.sha256)?,
        artifact.sha256.clone(),
        output.source.head.revision_id,
    ))
}
