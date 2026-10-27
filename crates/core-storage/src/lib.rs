//! Native `.deep3d` package persistence, immutable revisions and bounded content storage.
//!
//! This is a local single-branch engineering repository, not the cloud publication
//! coordinator. A persistent adjacent lock serializes writers; each transaction
//! reloads the package and compares both revision and generation before saving.

use core_domain::{
    apply_transaction, canonical_bytes, digest_bytes, CoreError, Project, Result, Transaction,
};
use fs2::FileExt;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};
use tempfile::NamedTempFile;
use uuid::Uuid;
use zip::{write::SimpleFileOptions, CompressionMethod, ZipArchive, ZipWriter};

pub const MAX_ARCHIVE_BYTES: u64 = 256 * 1024 * 1024;
pub const MAX_BLOB_BYTES: u64 = 64 * 1024 * 1024;
pub const MAX_METADATA_BYTES: u64 = 4 * 1024 * 1024;
pub const MAX_ENTRIES: usize = 10_000;
const PROJECT_SCHEMA: &str = "deep3d.project/1";
const MATERIALIZATION_SCHEMA: &str = "deep3d.materialization/1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct HeadToken {
    pub revision_id: Uuid,
    #[serde(with = "core_domain::u64_string")]
    pub generation: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Revision {
    pub id: Uuid,
    pub parent: Option<Uuid>,
    pub spec_hash: String,
    pub transaction_id: Option<Uuid>,
    pub transaction_hash: Option<String>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub project: Project,
    pub revision: Revision,
    /// Current branch token, also when `revision` is a historical snapshot.
    pub head: HeadToken,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactRef {
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Materialization {
    pub schema_version: String,
    pub project_id: Uuid,
    pub revision_id: Uuid,
    /// Hash of canonical ExecutionPlan bytes, not the pretty printed plan file.
    pub plan_hash: String,
    pub engine: BTreeMap<String, String>,
    pub files: BTreeMap<String, ArtifactRef>,
    pub feature_outputs: BTreeMap<Uuid, String>,
    pub node_outputs: BTreeMap<Uuid, String>,
}

#[derive(Debug, Clone)]
pub struct ProjectStore {
    path: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema_version: String,
    project_id: Uuid,
    head: HeadToken,
    revisions: BTreeMap<Uuid, Revision>,
    materializations: BTreeSet<String>,
}

struct State {
    manifest: Manifest,
    objects: BTreeMap<String, Vec<u8>>,
}

fn error(code: &str, message: impl Into<String>) -> CoreError {
    CoreError::new(code, message.into())
}

fn io_error(context: &str, cause: impl std::fmt::Display) -> CoreError {
    error("PROJECT_IO", format!("{context}: {cause}"))
}

fn hash_valid(hash: &str) -> bool {
    hash.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|ch| ch.is_ascii_digit() || (b'a'..=b'f').contains(&ch))
    })
}

fn checked_hash(hash: &str) -> Result<()> {
    if hash_valid(hash) {
        Ok(())
    } else {
        Err(error(
            "PROJECT_CORRUPT",
            "Invalid SHA-256 content reference",
        ))
    }
}

fn canonical_decode<T: DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    if bytes.len() as u64 > MAX_METADATA_BYTES {
        return Err(error("PROJECT_TOO_LARGE", "Metadata exceeds 4 MiB"));
    }
    let value: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|cause| error("PROJECT_CORRUPT", format!("Invalid metadata JSON: {cause}")))?;
    // Besides freezing encoding, this rejects duplicate map keys and alternate
    // number spellings that a generic JSON parser would otherwise discard.
    if canonical_bytes(&value)? != bytes {
        return Err(error(
            "PROJECT_CORRUPT",
            "Metadata must use the canonical project encoding",
        ));
    }
    serde_json::from_value(value).map_err(|cause| {
        error(
            "PROJECT_CORRUPT",
            format!("Invalid metadata contract: {cause}"),
        )
    })
}

fn object<'a>(state: &'a State, hash: &str) -> Result<&'a [u8]> {
    checked_hash(hash)?;
    state
        .objects
        .get(hash)
        .map(Vec::as_slice)
        .ok_or_else(|| error("PROJECT_CORRUPT", format!("Missing content object {hash}")))
}

fn snapshot(state: &State, id: Uuid) -> Result<Snapshot> {
    let revision = state
        .manifest
        .revisions
        .get(&id)
        .cloned()
        .ok_or_else(|| error("REVISION_NOT_FOUND", format!("Unknown revision {id}")))?;
    let project: Project = canonical_decode(object(state, &revision.spec_hash)?)?;
    Ok(Snapshot {
        project,
        revision,
        head: state.manifest.head.clone(),
    })
}

fn safe_logical_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 256
        && !name.contains(['\\', ':', '\0'])
        && !name.starts_with('/')
        && name
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
        && Path::new(name)
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
}

fn json_artifact(state: &State, record: &Materialization, name: &str) -> Result<serde_json::Value> {
    let reference = record.files.get(name).ok_or_else(|| {
        error(
            "MATERIALIZATION_INVALID",
            format!("Missing required artifact {name}"),
        )
    })?;
    let bytes = object(state, &reference.sha256)?;
    if bytes.len() as u64 > MAX_METADATA_BYTES {
        return Err(error(
            "PROJECT_TOO_LARGE",
            format!("{name} exceeds metadata limit"),
        ));
    }
    serde_json::from_slice(bytes).map_err(|cause| {
        error(
            "MATERIALIZATION_INVALID",
            format!("Invalid {name}: {cause}"),
        )
    })
}

fn same_json(left: &serde_json::Value, right: &serde_json::Value) -> Result<bool> {
    // JCS identifies a finite numeric quantity independently of whether its
    // JSON writer emitted 100 or 100.0. Value's internal Number variants do not.
    Ok(canonical_bytes(left)? == canonical_bytes(right)?)
}

fn validate_materialization(state: &State, record: &Materialization) -> Result<BTreeSet<String>> {
    if record.schema_version != MATERIALIZATION_SCHEMA {
        return Err(error(
            "SCHEMA_UNSUPPORTED",
            "Unsupported materialization schema",
        ));
    }
    if record.project_id != state.manifest.project_id {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Materialization belongs to another project",
        ));
    }
    let current = snapshot(state, record.revision_id)?;
    let plan = current.project.compile(record.revision_id)?;
    let plan_bytes = canonical_bytes(&plan)?;
    if digest_bytes(&plan_bytes) != record.plan_hash
        || object(state, &record.plan_hash)? != plan_bytes
    {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Canonical execution plan does not match revision",
        ));
    }
    if record.engine.is_empty()
        || record.engine.iter().any(|(key, value)| {
            key.is_empty() || key.len() > 128 || value.is_empty() || value.len() > 1024
        })
    {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Missing or invalid engine identity",
        ));
    }
    let mut reachable = BTreeSet::from([record.plan_hash.clone()]);
    for (name, reference) in &record.files {
        if !safe_logical_name(name) {
            return Err(error(
                "MATERIALIZATION_INVALID",
                "Unsafe artifact logical path",
            ));
        }
        let bytes = object(state, &reference.sha256)?;
        if bytes.len() as u64 != reference.size_bytes
            || (bytes.is_empty() && !name.ends_with(".log"))
        {
            return Err(error(
                "MATERIALIZATION_INVALID",
                format!("Artifact size mismatch for {name}"),
            ));
        }
        reachable.insert(reference.sha256.clone());
    }
    let plan_value = serde_json::to_value(&plan)
        .map_err(|cause| error("MATERIALIZATION_INVALID", cause.to_string()))?;
    if !same_json(&json_artifact(state, record, "plan.json")?, &plan_value)?
        || !same_json(
            &json_artifact(state, record, "goal.json")?,
            &plan_value["acceptance"],
        )?
        || !same_json(
            &json_artifact(state, record, "precision.json")?,
            &plan_value["precision"],
        )?
    {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Input artifacts are not bound to this plan",
        ));
    }
    if json_artifact(state, record, "build.json")?["status"] != "pass" {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Geometry build was not successful",
        ));
    }
    let mechanical = matches!(plan.acceptance, core_domain::Acceptance::Mechanical(_));
    for (name, stage) in [("verify-brep.json", "native"), ("verify-step.json", "step")] {
        let report = json_artifact(state, record, name)?;
        let geometry = if mechanical && stage == "native" {
            "model.brep"
        } else if mechanical {
            "model.step"
        } else if stage == "native" {
            "flange.brep"
        } else {
            "flange.step"
        };
        let artifact = record
            .files
            .get(geometry)
            .ok_or_else(|| error("MATERIALIZATION_INVALID", "Missing checked geometry"))?;
        let validator = record
            .engine
            .get("verifier_source_sha256")
            .ok_or_else(|| error("MATERIALIZATION_INVALID", "Missing verifier build identity"))?;
        checked_hash(validator)?;
        let checks = report["checks"].as_array().ok_or_else(|| {
            error(
                "MATERIALIZATION_INVALID",
                format!("Missing checks in {name}"),
            )
        })?;
        let schema = if mechanical {
            "deep3d.mechanical-verification/1"
        } else {
            "deep3d.flange-verification/1"
        };
        let validator_id = if mechanical {
            "deep3d.mechanical@1"
        } else {
            "deep3d.analytic-flange@1"
        };
        if report["schema_version"] != schema
            || report["status"] != "pass"
            || report["stage"] != stage
            || report["input_artifact_sha256"] != artifact.sha256
            || report["validator_id"] != validator_id
            || report["validator_source_sha256"] != *validator
            || !same_json(&report["expected_spec"], &plan_value["acceptance"])?
            || !same_json(&report["precision_profile"], &plan_value["precision"])?
            || checks.is_empty()
            || checks.iter().any(|check| check["status"] != "pass")
        {
            return Err(error(
                "MATERIALIZATION_INVALID",
                format!("Unbound or failed evidence in {name}"),
            ));
        }
        if mechanical && stage == "native" {
            let assembly = record.files.get("assembly.json").ok_or_else(|| {
                error(
                    "MATERIALIZATION_INVALID",
                    "Missing checked assembly metadata",
                )
            })?;
            let expected = serde_json::to_value(&record.node_outputs)
                .map_err(|cause| error("MATERIALIZATION_INVALID", cause.to_string()))?;
            if !same_json(&report["input_node_artifact_sha256"], &expected)?
                || report["input_assembly_artifact_sha256"] != assembly.sha256
            {
                return Err(error(
                    "MATERIALIZATION_INVALID",
                    "Verification is not bound to node artifacts and assembly metadata",
                ));
            }
        }
    }
    let expected_features: BTreeSet<Uuid> = current.project.features.keys().copied().collect();
    if record
        .feature_outputs
        .keys()
        .copied()
        .collect::<BTreeSet<_>>()
        != expected_features
    {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Feature output identities do not match plan",
        ));
    }
    for (id, hash) in &record.feature_outputs {
        let name = format!("features/{id}.brep");
        if record.files.get(&name).map(|item| &item.sha256) != Some(hash) {
            return Err(error(
                "MATERIALIZATION_INVALID",
                "Missing feature geometry artifact",
            ));
        }
    }
    let expected_node_outputs: BTreeMap<Uuid, String> = plan
        .outputs
        .iter()
        .map(|(node, feature)| {
            record
                .feature_outputs
                .get(feature)
                .cloned()
                .map(|hash| (*node, hash))
                .ok_or_else(|| {
                    error(
                        "MATERIALIZATION_INVALID",
                        "Scene binding lacks feature output",
                    )
                })
        })
        .collect::<Result<_>>()?;
    if record.node_outputs != expected_node_outputs
        || (!mechanical && record.node_outputs.len() != 1)
    {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Scene geometry bindings do not match plan",
        ));
    }
    if mechanical {
        for (node, hash) in &record.node_outputs {
            if record
                .files
                .get(&format!("nodes/{node}.brep"))
                .map(|file| &file.sha256)
                != Some(hash)
            {
                return Err(error("MATERIALIZATION_INVALID", "Missing bound node BREP"));
            }
        }
        let assembly = json_artifact(state, record, "assembly.json")?;
        if assembly["schema_version"] != "deep3d.rigid-assembly/1"
            || assembly["project_id"] != plan.project_id.to_string()
            || assembly["revision_id"] != plan.revision_id.to_string()
            || !same_json(&assembly["scene"], &plan_value["scene"])?
            || !record.files.contains_key("model.brep")
            || !record.files.contains_key("model.step")
        {
            return Err(error(
                "MATERIALIZATION_INVALID",
                "Missing or unbound assembly artifacts",
            ));
        }
        return Ok(reachable);
    }
    let final_hash = record
        .node_outputs
        .values()
        .next()
        .expect("one output checked");
    if record.files.get("flange.brep").map(|item| &item.sha256) != Some(final_hash)
        || !record.files.contains_key("flange.step")
    {
        return Err(error(
            "MATERIALIZATION_INVALID",
            "Missing bound final BREP/STEP artifacts",
        ));
    }
    Ok(reachable)
}

fn ordered_revisions(state: &State) -> Result<Vec<Revision>> {
    let mut result = Vec::new();
    let mut seen = BTreeSet::new();
    let mut next = Some(state.manifest.head.revision_id);
    while let Some(id) = next {
        if !seen.insert(id) {
            return Err(error(
                "PROJECT_CORRUPT",
                "Revision history contains a cycle",
            ));
        }
        let revision = state.manifest.revisions.get(&id).ok_or_else(|| {
            error(
                "PROJECT_CORRUPT",
                "Revision parent or branch head is missing",
            )
        })?;
        if revision.id != id {
            return Err(error(
                "PROJECT_CORRUPT",
                "Revision map key does not match identity",
            ));
        }
        result.push(revision.clone());
        next = revision.parent;
    }
    if result.len() != state.manifest.revisions.len()
        || result.len().checked_sub(1).map(|n| n as u64) != Some(state.manifest.head.generation)
    {
        return Err(error(
            "PROJECT_CORRUPT",
            "History is disconnected or generation is inconsistent",
        ));
    }
    result.reverse();
    Ok(result)
}

fn validate_state(state: &State) -> Result<()> {
    if state.manifest.schema_version != PROJECT_SCHEMA {
        return Err(error(
            "SCHEMA_UNSUPPORTED",
            "Unsupported native project schema",
        ));
    }
    if state.manifest.project_id.is_nil() {
        return Err(error("PROJECT_CORRUPT", "Project identity cannot be nil"));
    }
    let history = ordered_revisions(state)?;
    let mut reachable = BTreeSet::new();
    let mut transactions = BTreeSet::new();
    let mut previous: Option<Project> = None;
    for (generation, revision) in history.iter().enumerate() {
        let project: Project = canonical_decode(object(state, &revision.spec_hash)?)?;
        project.validate()?;
        if project.project_id != state.manifest.project_id || revision.id.is_nil() {
            return Err(error(
                "PROJECT_CORRUPT",
                "Revision has a foreign project or nil identity",
            ));
        }
        reachable.insert(revision.spec_hash.clone());
        match (&revision.transaction_id, &revision.transaction_hash) {
            (None, None) if generation == 0 => {}
            (Some(id), Some(hash)) if generation > 0 => {
                let transaction: Transaction = canonical_decode(object(state, hash)?)?;
                if *id != transaction.id
                    || transaction.id.is_nil()
                    || !transactions.insert(transaction.id)
                    || Some(transaction.base_revision) != revision.parent
                    || transaction.expected_generation != generation as u64 - 1
                    || transaction.message != revision.message
                {
                    return Err(error(
                        "PROJECT_CORRUPT",
                        "Invalid transaction history binding",
                    ));
                }
                let replayed =
                    apply_transaction(previous.as_ref().expect("parent checked"), &transaction)?;
                if canonical_bytes(&replayed)? != canonical_bytes(&project)? {
                    return Err(error(
                        "PROJECT_CORRUPT",
                        "Revision cannot be reproduced from its transaction",
                    ));
                }
                reachable.insert(hash.clone());
            }
            _ => {
                return Err(error(
                    "PROJECT_CORRUPT",
                    "Invalid initial or successor transaction",
                ))
            }
        }
        previous = Some(project);
    }
    for hash in &state.manifest.materializations {
        let record: Materialization = canonical_decode(object(state, hash)?)?;
        reachable.insert(hash.clone());
        reachable.extend(validate_materialization(state, &record)?);
    }
    if reachable != state.objects.keys().cloned().collect() {
        return Err(error(
            "PROJECT_CORRUPT",
            "Package contains unreachable content objects",
        ));
    }
    Ok(())
}

fn load(path: &Path) -> Result<State> {
    let file = File::open(path).map_err(|cause| io_error("Open project", cause))?;
    if file
        .metadata()
        .map_err(|cause| io_error("Read project size", cause))?
        .len()
        > MAX_ARCHIVE_BYTES
    {
        return Err(error("PROJECT_TOO_LARGE", "Native package exceeds 256 MiB"));
    }
    let mut archive = ZipArchive::new(file)
        .map_err(|cause| error("PROJECT_CORRUPT", format!("Invalid ZIP container: {cause}")))?;
    if archive.len() > MAX_ENTRIES {
        return Err(error(
            "PROJECT_TOO_LARGE",
            "Native package contains too many entries",
        ));
    }
    let mut manifest = None;
    let mut objects = BTreeMap::new();
    let mut names = BTreeSet::new();
    let mut total = 0u64;
    for index in 0..archive.len() {
        let entry = archive
            .by_index(index)
            .map_err(|cause| error("PROJECT_CORRUPT", format!("Invalid ZIP entry: {cause}")))?;
        let name = entry.name().to_owned();
        if !names.insert(name.clone()) || entry.is_dir() || entry.is_symlink() {
            return Err(error(
                "PROJECT_CORRUPT",
                "Duplicate, directory or symbolic-link ZIP entry",
            ));
        }
        let limit = if name == "manifest.json" {
            MAX_METADATA_BYTES
        } else {
            MAX_BLOB_BYTES
        };
        if entry.size() > limit {
            return Err(error(
                "PROJECT_TOO_LARGE",
                "ZIP entry exceeds declared size limit",
            ));
        }
        total = total
            .checked_add(entry.size())
            .ok_or_else(|| error("PROJECT_TOO_LARGE", "ZIP size overflow"))?;
        if total > MAX_ARCHIVE_BYTES {
            return Err(error(
                "PROJECT_TOO_LARGE",
                "Uncompressed native package exceeds 256 MiB",
            ));
        }
        let declared_size = entry.size();
        let mut bytes = Vec::new();
        entry
            .take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|cause| {
                error(
                    "PROJECT_CORRUPT",
                    format!("ZIP payload/CRC failure: {cause}"),
                )
            })?;
        if bytes.len() as u64 != declared_size || bytes.len() as u64 > limit {
            return Err(error("PROJECT_CORRUPT", "ZIP uncompressed size mismatch"));
        }
        if name == "manifest.json" {
            manifest = Some(canonical_decode::<Manifest>(&bytes)?);
        } else if let Some(hex) = name.strip_prefix("objects/") {
            let hash = format!("sha256:{hex}");
            checked_hash(&hash)?;
            if digest_bytes(&bytes) != hash {
                return Err(error("PROJECT_CORRUPT", "Content object SHA-256 mismatch"));
            }
            objects.insert(hash, bytes);
        } else {
            return Err(error(
                "PROJECT_CORRUPT",
                format!("Unexpected ZIP path {name}"),
            ));
        }
    }
    let state = State {
        manifest: manifest
            .ok_or_else(|| error("PROJECT_CORRUPT", "Native package has no manifest"))?,
        objects,
    };
    validate_state(&state)?;
    Ok(state)
}

fn normalized_path(path: &Path, existing: bool) -> Result<PathBuf> {
    if existing {
        let resolved =
            fs::canonicalize(path).map_err(|cause| io_error("Resolve project path", cause))?;
        if !resolved.is_file() {
            return Err(error("PROJECT_IO", "Project path is not a file"));
        }
        return Ok(resolved);
    }
    let name = path
        .file_name()
        .ok_or_else(|| error("PROJECT_IO", "Project path needs a file name"))?;
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let parent =
        fs::canonicalize(parent).map_err(|cause| io_error("Resolve project parent", cause))?;
    Ok(parent.join(name))
}

fn acquire_lock(path: &Path) -> Result<File> {
    let mut name = path
        .file_name()
        .ok_or_else(|| error("PROJECT_IO", "Missing project file name"))?
        .to_os_string();
    name.push(".lock");
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path.with_file_name(name))
        .map_err(|cause| io_error("Open project lock", cause))?;
    FileExt::try_lock_exclusive(&lock).map_err(|cause| {
        if cause.kind() == std::io::ErrorKind::WouldBlock || cause.raw_os_error() == Some(33) {
            error("PROJECT_BUSY", "Another writer currently owns this project")
        } else {
            io_error("Lock project", cause)
        }
    })?;
    Ok(lock)
}

fn save(path: &Path, state: &State, create: bool) -> Result<()> {
    validate_state(state)?;
    if state.objects.len() + 1 > MAX_ENTRIES {
        return Err(error(
            "PROJECT_TOO_LARGE",
            "Too many native package entries",
        ));
    }
    let manifest = canonical_bytes(&state.manifest)?;
    let total = state
        .objects
        .values()
        .try_fold(manifest.len() as u64, |total, bytes| {
            if bytes.len() as u64 > MAX_BLOB_BYTES {
                return Err(error("PROJECT_TOO_LARGE", "Content object exceeds 64 MiB"));
            }
            total
                .checked_add(bytes.len() as u64)
                .ok_or_else(|| error("PROJECT_TOO_LARGE", "Content size overflow"))
        })?;
    if manifest.len() as u64 > MAX_METADATA_BYTES || total > MAX_ARCHIVE_BYTES {
        return Err(error(
            "PROJECT_TOO_LARGE",
            "Native package metadata/content exceeds limits",
        ));
    }
    let parent = path.parent().expect("normalized project path has a parent");
    let mut temporary = NamedTempFile::new_in(parent)
        .map_err(|cause| io_error("Create project staging file", cause))?;
    {
        let mut writer = ZipWriter::new(temporary.as_file_mut());
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Stored)
            .large_file(true);
        writer
            .start_file("manifest.json", options)
            .map_err(|cause| io_error("Write native manifest", cause))?;
        writer
            .write_all(&manifest)
            .map_err(|cause| io_error("Write manifest content", cause))?;
        for (hash, bytes) in &state.objects {
            checked_hash(hash)?;
            writer
                .start_file(format!("objects/{}", &hash[7..]), options)
                .map_err(|cause| io_error("Write content entry", cause))?;
            writer
                .write_all(bytes)
                .map_err(|cause| io_error("Write content object", cause))?;
        }
        writer
            .finish()
            .map_err(|cause| io_error("Finish native package", cause))?;
    }
    if temporary
        .as_file()
        .metadata()
        .map_err(|cause| io_error("Staging size", cause))?
        .len()
        > MAX_ARCHIVE_BYTES
    {
        return Err(error(
            "PROJECT_TOO_LARGE",
            "Native ZIP container exceeds 256 MiB",
        ));
    }
    temporary
        .as_file()
        .sync_all()
        .map_err(|cause| io_error("Persist project staging data", cause))?;
    if create {
        temporary.persist_noclobber(path).map_err(|cause| {
            if cause.error.kind() == std::io::ErrorKind::AlreadyExists {
                error(
                    "PROJECT_EXISTS",
                    "An existing project cannot be overwritten by create",
                )
            } else {
                io_error("Create native project", cause.error)
            }
        })?;
    } else {
        temporary
            .persist(path)
            .map_err(|cause| io_error("Replace native project atomically", cause.error))?;
    }
    #[cfg(unix)]
    File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|cause| {
            error(
                "COMMIT_DURABILITY_UNKNOWN",
                format!("Rename succeeded but directory sync failed: {cause}"),
            )
        })?;
    Ok(())
}

impl ProjectStore {
    pub fn create(path: impl AsRef<Path>, project: &Project) -> Result<Snapshot> {
        project.validate()?;
        let path = normalized_path(path.as_ref(), false)?;
        let _lock = acquire_lock(&path)?;
        if path.exists() {
            return Err(error("PROJECT_EXISTS", "Project already exists"));
        }
        let bytes = canonical_bytes(project)?;
        let spec_hash = digest_bytes(&bytes);
        let revision = Revision {
            id: Uuid::new_v4(),
            parent: None,
            spec_hash: spec_hash.clone(),
            transaction_id: None,
            transaction_hash: None,
            message: "Initial project".to_owned(),
        };
        let state = State {
            manifest: Manifest {
                schema_version: PROJECT_SCHEMA.to_owned(),
                project_id: project.project_id,
                head: HeadToken {
                    revision_id: revision.id,
                    generation: 0,
                },
                revisions: BTreeMap::from([(revision.id, revision.clone())]),
                materializations: BTreeSet::new(),
            },
            objects: BTreeMap::from([(spec_hash, bytes)]),
        };
        save(&path, &state, true)?;
        snapshot(&state, revision.id)
    }

    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = normalized_path(path.as_ref(), true)?;
        load(&path)?;
        Ok(Self { path })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn head(&self) -> Result<Snapshot> {
        let state = load(&self.path)?;
        snapshot(&state, state.manifest.head.revision_id)
    }

    pub fn read_revision(&self, id: Uuid) -> Result<Snapshot> {
        snapshot(&load(&self.path)?, id)
    }

    pub fn history(&self) -> Result<Vec<Revision>> {
        ordered_revisions(&load(&self.path)?)
    }

    pub fn commit(&self, transaction: &Transaction) -> Result<Snapshot> {
        let _lock = acquire_lock(&self.path)?;
        let mut state = load(&self.path)?;
        let transaction_bytes = canonical_bytes(transaction)?;
        let transaction_hash = digest_bytes(&transaction_bytes);
        if let Some(previous) = state
            .manifest
            .revisions
            .values()
            .find(|r| r.transaction_id == Some(transaction.id))
        {
            if previous.transaction_hash.as_deref() != Some(&transaction_hash) {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Transaction identity was reused with different content",
                ));
            }
            return snapshot(&state, previous.id);
        }
        if transaction.base_revision != state.manifest.head.revision_id
            || transaction.expected_generation != state.manifest.head.generation
        {
            return Err(error(
                "REVISION_CONFLICT",
                "Expected revision and generation no longer match branch head",
            ));
        }
        let previous = snapshot(&state, transaction.base_revision)?;
        let project = apply_transaction(&previous.project, transaction)?;
        let spec_bytes = canonical_bytes(&project)?;
        let spec_hash = digest_bytes(&spec_bytes);
        let revision = Revision {
            id: Uuid::new_v4(),
            parent: Some(transaction.base_revision),
            spec_hash: spec_hash.clone(),
            transaction_id: Some(transaction.id),
            transaction_hash: Some(transaction_hash.clone()),
            message: transaction.message.clone(),
        };
        state.manifest.head = HeadToken {
            revision_id: revision.id,
            generation: state
                .manifest
                .head
                .generation
                .checked_add(1)
                .ok_or_else(|| error("GENERATION_EXHAUSTED", "Branch generation cannot wrap"))?,
        };
        state
            .manifest
            .revisions
            .insert(revision.id, revision.clone());
        state.objects.insert(transaction_hash, transaction_bytes);
        state.objects.insert(spec_hash, spec_bytes);
        save(&self.path, &state, false)?;
        snapshot(&state, revision.id)
    }

    pub fn attach_materialization(
        &self,
        record: Materialization,
        blobs: BTreeMap<String, Vec<u8>>,
    ) -> Result<String> {
        let _lock = acquire_lock(&self.path)?;
        let mut state = load(&self.path)?;
        for (hash, bytes) in blobs {
            checked_hash(&hash)?;
            if bytes.len() as u64 > MAX_BLOB_BYTES || digest_bytes(&bytes) != hash {
                return Err(error(
                    "MATERIALIZATION_INVALID",
                    "Supplied content hash/size does not match bytes",
                ));
            }
            state.objects.entry(hash).or_insert(bytes);
        }
        validate_materialization(&state, &record)?;
        let bytes = canonical_bytes(&record)?;
        let hash = digest_bytes(&bytes);
        if state.manifest.materializations.contains(&hash) {
            return Ok(hash);
        }
        state.objects.insert(hash.clone(), bytes);
        state.manifest.materializations.insert(hash.clone());
        save(&self.path, &state, false)?;
        Ok(hash)
    }

    pub fn materializations(&self, revision_id: Uuid) -> Result<Vec<(String, Materialization)>> {
        let state = load(&self.path)?;
        snapshot(&state, revision_id)?;
        state
            .manifest
            .materializations
            .iter()
            .filter_map(|hash| {
                let parsed = canonical_decode::<Materialization>(object(&state, hash).ok()?);
                match parsed {
                    Ok(record) if record.revision_id == revision_id => {
                        Some(Ok((hash.clone(), record)))
                    }
                    Ok(_) => None,
                    Err(cause) => Some(Err(cause)),
                }
            })
            .collect()
    }

    pub fn read_blob(&self, hash: &str) -> Result<Vec<u8>> {
        let state = load(&self.path)?;
        Ok(object(&state, hash)?.to_vec())
    }
}

mod candidate;

#[cfg(test)]
mod candidate_tests;
#[cfg(test)]
mod tests;
