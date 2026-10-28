mod artifacts;
mod authority_http;
mod publication;

use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use clap::{Parser, Subcommand};
use core_domain::{CoreError, Result};
use core_runtime::*;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::sync::Semaphore;
use uuid::Uuid;

#[derive(Parser)]
#[command(
    name = "deep3d-runtime",
    about = "Durable CAD runs and concurrent worker coordination"
)]
struct Cli {
    #[command(subcommand)]
    action: Action,
}

#[derive(Subcommand)]
enum Action {
    Migrate,
    Serve {
        #[arg(long, default_value = "127.0.0.1:8088")]
        bind: std::net::SocketAddr,
        #[arg(long)]
        root: PathBuf,
        #[arg(long, default_value_t = 30, value_parser = clap::value_parser!(i32).range(2..=300))]
        lease_seconds: i32,
    },
}

#[derive(Clone)]
struct App {
    repository: Repository,
    root: Arc<PathBuf>,
    operator: Arc<String>,
    worker: Arc<String>,
    slots: Arc<Semaphore>,
    verifier_python: Arc<PathBuf>,
}

struct ApiError(CoreError);
impl From<CoreError> for ApiError {
    fn from(value: CoreError) -> Self {
        Self(value)
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let code = match self.0.code.as_str() {
            "UNAUTHORIZED" => StatusCode::UNAUTHORIZED,
            "RUN_NOT_FOUND" | "PROJECT_NOT_FOUND" | "GOAL_NOT_FOUND" | "COMMIT_NOT_FOUND"
            | "PLAN_NOT_FOUND" => StatusCode::NOT_FOUND,
            "REVISION_CONFLICT"
            | "IDEMPOTENCY_CONFLICT"
            | "STALE_LEASE"
            | "STALE_CONTROL"
            | "CONTROL_CONFLICT"
            | "STALE_EXECUTION_LEASE"
            | "RUN_TERMINAL"
            | "TASK_ALREADY_COMPLETED" | "GOAL_CONFLICT" | "PUBLIC_HEAD_CONFLICT"
            | "GOAL_MISMATCH" | "GOAL_UNRESOLVED" | "PROJECT_CONFLICT"
            | "PUBLICATION_CONFLICT" | "PUBLIC_NOT_READY" => StatusCode::CONFLICT,
            "DATABASE_UNAVAILABLE"
            | "DATABASE_BUSY"
            | "COMMIT_OUTCOME_UNKNOWN"
            | "SERVICE_STOPPING"
            | "SERVICE_ERROR" => StatusCode::SERVICE_UNAVAILABLE,
            _ => StatusCode::BAD_REQUEST,
        };
        (code, Json(json!({"status":"error", "error":self.0}))).into_response()
    }
}

fn envelope<T: serde::Serialize>(value: T) -> Json<Value> {
    Json(json!({"status":"pass", "result":value}))
}

fn equal_secret(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0u8, |diff, (a, b)| diff | (a ^ b))
        == 0
}

fn authorize(app: &App, headers: &HeaderMap, worker: bool) -> Result<()> {
    let supplied = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .unwrap_or("");
    let secret = if worker { &app.worker } else { &app.operator };
    if !equal_secret(supplied.as_bytes(), secret.as_bytes()) {
        return Err(CoreError::new(
            "UNAUTHORIZED",
            "A valid role-specific bearer token is required",
        ));
    }
    Ok(())
}

async fn blocking<T: Send + 'static>(
    app: &App,
    operation: impl FnOnce() -> Result<T> + Send + 'static,
) -> std::result::Result<T, ApiError> {
    let permit = app
        .slots
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| CoreError::new("SERVICE_STOPPING", "Runtime is stopping"))?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        operation()
    })
    .await
    .map_err(|_| CoreError::new("SERVICE_ERROR", "Runtime operation did not return"))?
    .map_err(Into::into)
}

async fn health() -> Json<Value> {
    envelope(json!({"service":"deep3d-runtime"}))
}

async fn submit(
    State(app): State<App>,
    headers: HeaderMap,
    Json(request): Json<SubmitRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    if request.schema_version != "deep3d.submit-run/1" || request.run_id.is_nil() {
        return Err(CoreError::new(
            "RUN_INVALID",
            "Expected deep3d.submit-run/1 and a stable run UUID",
        )
        .into());
    }
    validate_tasks(&request.tasks)?;
    let repository = app.repository.clone();
    let root = app.root.clone();
    let result = blocking(&app, move || {
        let digest = request_digest(&request)?;
        match repository.get_run(request.run_id) {
            Ok(existing) => {
                if existing.spec.submission_sha256 != digest {
                    return Err(CoreError::new(
                        "IDEMPOTENCY_CONFLICT",
                        "Run identity has a different submission",
                    ));
                }
                return Ok(existing);
            }
            Err(error) if error.code == "RUN_NOT_FOUND" => {}
            Err(error) => return Err(error),
        }
        let (project_id, source) = artifacts::freeze(&root, &request)?;
        repository.submit(&RunSpec {
            schema_version: "deep3d.durable-run/1".into(),
            run_id: request.run_id,
            project_id,
            submission_sha256: digest,
            source,
            tasks: request.tasks,
            authority: request.authority,
        })
    })
    .await?;
    Ok(envelope(result))
}

async fn import(
    State(app): State<App>,
    headers: HeaderMap,
    bytes: Bytes,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let root = app.root.clone();
    let (project_id, source) =
        blocking(&app, move || artifacts::import_native(&root, &bytes)).await?;
    Ok(envelope(json!({"project_id":project_id,"source":source})))
}

async fn run(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<Uuid>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.get_run(id)).await?,
    ))
}

#[derive(Deserialize)]
struct Cursor {
    #[serde(default)]
    after: i64,
    #[serde(default = "event_limit")]
    limit: i64,
}
fn event_limit() -> i64 {
    200
}
async fn events(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<Uuid>,
    Query(cursor): Query<Cursor>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || {
            repository.events(id, cursor.after, cursor.limit)
        })
        .await?,
    ))
}

async fn control(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<Uuid>,
    Json(request): Json<ControlRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.control(id, &request)).await?,
    ))
}

async fn claim(
    State(app): State<App>,
    headers: HeaderMap,
    Json(request): Json<ClaimRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, true)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.claim(&request)).await?,
    ))
}

async fn heartbeat(
    State(app): State<App>,
    headers: HeaderMap,
    Json(token): Json<LeaseToken>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, true)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.heartbeat(&token)).await?,
    ))
}

async fn complete(
    State(app): State<App>,
    headers: HeaderMap,
    Json(request): Json<CompletionRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, true)?;
    let repository = app.repository.clone();
    let root = app.root.clone();
    let python = app.verifier_python.clone();
    Ok(envelope(
        blocking(&app, move || {
            let digest = request_digest(&request)?;
            if let Some(output) = repository.completion(&request.lease, &digest)? {
                return Ok(output);
            }
            let source = repository.source_for(&request.lease)?;
            let run = repository.get_run(request.lease.run_id)?;
            let task = run
                .spec
                .tasks
                .iter()
                .find(|task| task.key == request.lease.task_key)
                .ok_or_else(|| CoreError::new("TASK_NOT_FOUND", "Unknown task"))?;
            let output = artifacts::verify_result(
                &root,
                &request.lease,
                &source,
                task,
                &request.materialization,
            )?;
            // File verification/copy is outside the DB transaction. This final operation
            // rechecks the complete fencing tuple after pause, cancellation or expiry.
            let evidence = if let Some(binding) = &run.spec.authority {
                if binding.publish_task == request.lease.task_key {
                    let goal = repository.get_goal(run.spec.project_id, binding.goal_revision_id)?;
                    Some(publication::verify(&root, &python, &goal, &output)?)
                } else {
                    None
                }
            } else {
                None
            };
            repository.complete_with_publication(&request.lease, &output, &digest, evidence.as_ref())
        })
        .await?,
    ))
}

async fn fail(
    State(app): State<App>,
    headers: HeaderMap,
    Json(request): Json<FailureRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, true)?;
    let repository = app.repository.clone();
    blocking(&app, move || repository.fail(&request)).await?;
    Ok(envelope(Value::Null))
}

async fn download(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, key, kind)): Path<(Uuid, String, String)>,
) -> std::result::Result<Response, ApiError> {
    authorize(&app, &headers, false)?;
    if !valid_key(&key) || !["step", "native"].contains(&kind.as_str()) {
        return Err(
            CoreError::new("ARTIFACT_PATH", "Select a task's step or native artifact").into(),
        );
    }
    let repository = app.repository.clone();
    let root = app.root.clone();
    let (bytes, digest, revision) = blocking(&app, move || {
        let run = repository.get_run(id)?;
        let output = run
            .tasks
            .into_iter()
            .find(|task| task.spec.key == key)
            .and_then(|task| task.output)
            .ok_or_else(|| CoreError::new("ARTIFACT_NOT_READY", "Task has no accepted output"))?;
        artifacts::download(&root, &output, &kind)
    })
    .await?;
    let mut headers = HeaderMap::new();
    headers.insert("content-type", "application/octet-stream".parse().unwrap());
    headers.insert(
        "x-content-sha256",
        digest
            .parse()
            .map_err(|_| CoreError::new("RUNTIME_CORRUPT", "Invalid digest"))?,
    );
    headers.insert("x-spec-revision", revision.to_string().parse().unwrap());
    Ok((headers, bytes).into_response())
}

fn token(name: &str) -> Result<String> {
    let value = std::env::var(name).unwrap_or_default();
    if value.len() < 24 || !value.bytes().all(|c| (33..=126).contains(&c)) {
        return Err(CoreError::new(
            "SERVICE_CONFIG",
            format!("Set {name} to a token of at least 24 printable characters"),
        ));
    }
    Ok(value)
}

async fn execute(cli: Cli) -> Result<()> {
    let database = std::env::var("DEEP3D_DATABASE_URL")
        .map_err(|_| CoreError::new("SERVICE_CONFIG", "Set DEEP3D_DATABASE_URL"))?;
    let lease_seconds = match cli.action {
        Action::Serve { lease_seconds, .. } => lease_seconds,
        _ => 30,
    };
    let repository = Repository::new(&database, lease_seconds)?;
    let migration = repository.clone();
    tokio::task::spawn_blocking(move || migration.migrate())
        .await
        .map_err(|_| CoreError::new("SERVICE_ERROR", "Migration worker stopped"))??;
    let Action::Serve { bind, root, .. } = cli.action else {
        println!(
            "{}",
            json!({"status":"pass","result":{"migration":"current"}})
        );
        return Ok(());
    };
    let operator = token("DEEP3D_OPERATOR_TOKEN")?;
    let worker = token("DEEP3D_WORKER_TOKEN")?;
    if equal_secret(operator.as_bytes(), worker.as_bytes()) {
        return Err(CoreError::new(
            "SERVICE_CONFIG",
            "Operator and worker tokens must differ",
        ));
    }
    std::fs::create_dir_all(&root)
        .map_err(|_| CoreError::new("SERVICE_CONFIG", "Cannot create artifact root"))?;
    let root = root
        .canonicalize()
        .map_err(|_| CoreError::new("SERVICE_CONFIG", "Cannot resolve artifact root"))?;
    let app = App {
        repository: repository.clone(),
        root: Arc::new(root),
        operator: Arc::new(operator),
        worker: Arc::new(worker),
        slots: Arc::new(Semaphore::new(16)),
        verifier_python: Arc::new(publication::python()),
    };
    let api = Router::new()
        .route("/health", get(health))
        .route("/v1/projects", post(authority_http::register))
        .route("/v1/projects/{id}", get(authority_http::project))
        .route("/v1/projects/{id}/goals", post(authority_http::update_goal))
        .route("/v1/projects/{id}/goals/{goal}", get(authority_http::goal))
        .route("/v1/projects/{id}/plans/{run}", get(authority_http::plan))
        .route("/v1/projects/{id}/commits/{commit}", get(authority_http::commit))
        .route("/v1/projects/{id}/public/artifacts/{kind}", get(authority_http::public_artifact))
        .route("/v1/projects/{id}/commits/{commit}/artifacts/{kind}", get(authority_http::commit_artifact))
        .route("/v1/projects/{id}/commits/{commit}/evidence/{stage}", get(authority_http::evidence))
        .route("/v1/runs", post(submit))
        .route("/v1/runs/{id}", get(run))
        .route("/v1/runs/{id}/events", get(events))
        .route("/v1/runs/{id}/control", post(control))
        .route("/v1/runs/{id}/tasks/{key}/artifacts/{kind}", get(download))
        .route("/v1/worker/claim", post(claim))
        .route("/v1/worker/heartbeat", post(heartbeat))
        .route("/v1/worker/complete", post(complete))
        .route("/v1/worker/fail", post(fail))
        .layer(DefaultBodyLimit::max(256 * 1024))
        .route(
            "/v1/artifacts/native",
            post(import).layer(DefaultBodyLimit::max(16 * 1024 * 1024)),
        )
        .with_state(app);
    let recovery = tokio::spawn(async move {
        let mut timer = tokio::time::interval(Duration::from_secs(2));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            timer.tick().await;
            let repository = repository.clone();
            match tokio::task::spawn_blocking(move || repository.recover()).await {
                Ok(Ok(count)) if count > 0 => {
                    eprintln!("{}", json!({"event":"leases_recovered","count":count}))
                }
                Ok(Err(error)) => {
                    eprintln!("{}", json!({"event":"recovery_error","code":error.code}))
                }
                _ => {}
            }
        }
    });
    let listener = tokio::net::TcpListener::bind(bind)
        .await
        .map_err(|_| CoreError::new("SERVICE_CONFIG", "Cannot bind runtime address"))?;
    println!(
        "{}",
        json!({"event":"runtime_ready","bind":bind.to_string()})
    );
    let result = axum::serve(listener, api)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await;
    recovery.abort();
    result.map_err(|_| CoreError::new("SERVICE_ERROR", "HTTP runtime stopped unexpectedly"))
}

#[tokio::main]
async fn main() {
    if let Err(error) = execute(Cli::parse()).await {
        eprintln!("{}", json!({"status":"error","error":error}));
        std::process::exit(2);
    }
}
