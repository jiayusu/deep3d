mod bridge;

use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use clap::{Parser, Subcommand};
use core_domain::{canonical_bytes, Command, CoreError, Project, Result, ScalarValue, Transaction};
use core_storage::{ProjectStore, Snapshot};
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use uuid::Uuid;

#[derive(Parser)]
#[command(
    name = "deep3d",
    version,
    about = "Native CAD projects, typed features and immutable revisions"
)]
struct Cli {
    #[command(subcommand)]
    command: Action,
}

#[derive(Subcommand)]
enum Action {
    /// Create a self-contained native project with a parameterized flange DAG.
    Create {
        #[arg(long)]
        project: PathBuf,
        #[arg(long, default_value = "Flange project")]
        name: String,
    },
    /// Reopen the current or an immutable historical revision.
    Show {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        revision: Option<Uuid>,
    },
    /// Atomically edit parameters; omitted preconditions use a freshly read head.
    Edit {
        #[arg(long)]
        project: PathBuf,
        #[arg(long = "set", required = true)]
        assignments: Vec<String>,
        #[arg(long, requires = "expected_generation")]
        expected_revision: Option<Uuid>,
        #[arg(long, requires = "expected_revision")]
        expected_generation: Option<u64>,
        #[arg(long)]
        transaction_id: Option<Uuid>,
        #[arg(long, default_value = "Edit parameters")]
        message: String,
    },
    /// Apply a versioned, multi-command transaction with explicit CAS preconditions.
    Apply {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        transaction: PathBuf,
    },
    History {
        #[arg(long)]
        project: PathBuf,
    },
    /// Compile definitions to an explicit typed OCCT execution plan.
    Plan {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        revision: Option<Uuid>,
        #[arg(long)]
        out: PathBuf,
    },
    /// Evaluate a frozen revision, independently verify and save all artifacts in the package.
    Evaluate {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        revision: Option<Uuid>,
        #[arg(long)]
        python: Option<PathBuf>,
        #[arg(long)]
        out: PathBuf,
        #[arg(long, default_value_t = 120, value_parser = clap::value_parser!(u64).range(1..=600))]
        timeout: u64,
    },
    /// Export a verified historical artifact without invoking OCCT or overwriting a file.
    Export {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        materialization: String,
        #[arg(long, default_value = "flange.step")]
        file: String,
        #[arg(long)]
        out: PathBuf,
    },
}

fn io_error(error: impl std::fmt::Display) -> CoreError {
    CoreError::new("IO_ERROR", error.to_string())
}

fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let file = std::fs::File::open(path).map_err(io_error)?;
    let mut bytes = Vec::new();
    file.take(4 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() > 4 * 1024 * 1024 {
        return Err(CoreError::new(
            "INPUT_TOO_LARGE",
            "JSON input exceeds 4 MiB",
        ));
    }
    serde_json::from_slice(&bytes).map_err(|e| CoreError::new("SCHEMA_INVALID", e.to_string()))
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| {
            CoreError::new(
                if e.kind() == std::io::ErrorKind::AlreadyExists {
                    "OUTPUT_EXISTS"
                } else {
                    "IO_ERROR"
                },
                e.to_string(),
            )
        })?;
    file.write_all(bytes).map_err(io_error)?;
    file.sync_all().map_err(io_error)
}

fn snapshot(store: &ProjectStore, revision: Option<Uuid>) -> Result<Snapshot> {
    revision.map_or_else(|| store.head(), |id| store.read_revision(id))
}

fn json_value<T: serde::Serialize>(value: T) -> Result<Value> {
    serde_json::to_value(value).map_err(|e| CoreError::new("SERIALIZATION_FAILED", e.to_string()))
}

fn execute(action: Action) -> Result<Value> {
    match action {
        Action::Create { project, name } => {
            json_value(ProjectStore::create(project, &Project::new_flange(&name)?)?)
        }
        Action::Show { project, revision } => {
            let store = ProjectStore::open(project)?;
            let snapshot = snapshot(&store, revision)?;
            let materializations = store.materializations(snapshot.revision.id)?;
            Ok(json!({"snapshot": snapshot, "materializations": materializations}))
        }
        Action::Edit {
            project,
            assignments,
            expected_revision,
            expected_generation,
            transaction_id,
            message,
        } => {
            let store = ProjectStore::open(project)?;
            let current = store.head()?;
            let mut commands = Vec::new();
            let mut assigned = std::collections::BTreeSet::new();
            for assignment in assignments {
                let (name, text) = assignment.split_once('=').ok_or_else(|| {
                    CoreError::new("PARAMETER_INVALID", "Use --set parameter_name=value")
                })?;
                let id = current.project.parameter_id(name)?;
                if !assigned.insert(id) {
                    return Err(CoreError::new(
                        "PARAMETER_INVALID",
                        "Each parameter may be assigned once per edit",
                    ));
                }
                let value = text.parse::<f64>().map_err(|_| {
                    CoreError::new("PARAMETER_INVALID", format!("Invalid number for {name}"))
                })?;
                if !value.is_finite() {
                    return Err(CoreError::new(
                        "PARAMETER_INVALID",
                        "Parameter must be finite",
                    ));
                }
                let typed = match current.project.parameters[&id].value {
                    ScalarValue::LengthMm(_) => ScalarValue::LengthMm(value),
                    ScalarValue::AngleRad(_) => ScalarValue::AngleRad(value),
                    ScalarValue::Unitless(_) => ScalarValue::Unitless(value),
                };
                commands.push(Command::ParameterSet { id, value: typed });
            }
            let transaction = Transaction {
                schema_version: "deep3d.transaction/1".into(),
                id: transaction_id.unwrap_or_else(Uuid::new_v4),
                base_revision: expected_revision.unwrap_or(current.head.revision_id),
                expected_generation: expected_generation.unwrap_or(current.head.generation),
                message,
                commands,
            };
            json_value(store.commit(&transaction)?)
        }
        Action::Apply {
            project,
            transaction,
        } => json_value(
            ProjectStore::open(project)?.commit(&read_json::<Transaction>(&transaction)?)?,
        ),
        Action::History { project } => json_value(ProjectStore::open(project)?.history()?),
        Action::Plan {
            project,
            revision,
            out,
        } => {
            let store = ProjectStore::open(project)?;
            let current = snapshot(&store, revision)?;
            let plan = current.project.compile(current.revision.id)?;
            let bytes = canonical_bytes(&plan)?;
            write_new(&out, &bytes)?;
            Ok(
                json!({"revision_id": current.revision.id, "plan_sha256": core_domain::digest_bytes(&bytes), "output": out}),
            )
        }
        Action::Evaluate {
            project,
            revision,
            python,
            out,
            timeout,
        } => {
            let store = ProjectStore::open(project)?;
            let current = snapshot(&store, revision)?;
            bridge::evaluate(&store, &current, python.as_deref(), &out, timeout)
        }
        Action::Export {
            project,
            materialization,
            file,
            out,
        } => {
            let store = ProjectStore::open(project)?;
            let mut record = None;
            for revision in store.history()? {
                for (hash, candidate) in store.materializations(revision.id)? {
                    if hash == materialization {
                        record = Some(candidate);
                        break;
                    }
                }
            }
            let record = record.ok_or_else(|| {
                CoreError::new("MATERIALIZATION_NOT_FOUND", "Unknown materialization")
            })?;
            let artifact = record.files.get(&file).ok_or_else(|| {
                CoreError::new(
                    "ARTIFACT_NOT_FOUND",
                    "File is not in the verified materialization",
                )
            })?;
            let bytes = store.read_blob(&artifact.sha256)?;
            write_new(&out, &bytes)?;
            Ok(
                json!({"revision_id": record.revision_id, "materialization": materialization, "sha256": artifact.sha256, "output": out}),
            )
        }
    }
}

fn main() {
    match execute(Cli::parse().command) {
        Ok(value) => println!("{}", json!({"status": "pass", "result": value})),
        Err(error) => {
            println!("{}", json!({"status": "error", "error": error}));
            std::process::exit(2);
        }
    }
}
