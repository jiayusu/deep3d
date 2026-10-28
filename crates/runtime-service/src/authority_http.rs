use super::*;

pub(super) async fn register(
    State(app): State<App>,
    headers: HeaderMap,
    Json(request): Json<RegisterProjectRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    let root = app.root.clone();
    let result = blocking(&app, move || {
        let (id, source) =
            artifacts::freeze_reference(&root, &request.project, &request.expected_head)?;
        repository.register_project(id, &source, request.operation_id)
    })
    .await?;
    Ok(envelope(result))
}

pub(super) async fn project(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<Uuid>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.get_project(id)).await?,
    ))
}

pub(super) async fn update_goal(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<Uuid>,
    Json(request): Json<UpdateGoalRequest>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.update_goal(id, &request)).await?,
    ))
}

pub(super) async fn goal(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, goal)): Path<(Uuid, Uuid)>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.get_goal(id, goal)).await?,
    ))
}

pub(super) async fn plan(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, run)): Path<(Uuid, Uuid)>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.get_plan(id, run)).await?,
    ))
}

pub(super) async fn commit(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, commit)): Path<(Uuid, Uuid)>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    let repository = app.repository.clone();
    Ok(envelope(
        blocking(&app, move || repository.get_commit(id, commit)).await?,
    ))
}

fn artifact_response(bytes: Vec<u8>, digest: String, revision: Uuid) -> Result<Response> {
    let mut headers = HeaderMap::new();
    headers.insert("content-type", "application/octet-stream".parse().unwrap());
    headers.insert(
        "x-content-sha256",
        digest
            .parse()
            .map_err(|_| CoreError::new("RUNTIME_CORRUPT", "Invalid public artifact digest"))?,
    );
    headers.insert("x-spec-revision", revision.to_string().parse().unwrap());
    Ok((headers, bytes).into_response())
}

pub(super) async fn public_artifact(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, kind)): Path<(Uuid, String)>,
) -> std::result::Result<Response, ApiError> {
    authorize(&app, &headers, false)?;
    if !["step", "native"].contains(&kind.as_str()) {
        return Err(CoreError::new("ARTIFACT_PATH", "Select native or step").into());
    }
    let repository = app.repository.clone();
    let root = app.root.clone();
    blocking(&app, move || {
        let view = repository.get_project(id)?;
        let commit = view
            .public_commit
            .ok_or_else(|| CoreError::new("PUBLIC_NOT_READY", "No verified public commit"))?;
        let (bytes, digest, revision) = artifacts::download(&root, &commit.output, &kind)?;
        artifact_response(bytes, digest, revision)
    })
    .await
}

pub(super) async fn commit_artifact(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, commit, kind)): Path<(Uuid, Uuid, String)>,
) -> std::result::Result<Response, ApiError> {
    authorize(&app, &headers, false)?;
    if !["step", "native"].contains(&kind.as_str()) {
        return Err(CoreError::new("ARTIFACT_PATH", "Select native or step").into());
    }
    let repository = app.repository.clone();
    let root = app.root.clone();
    blocking(&app, move || {
        let commit = repository.get_commit(id, commit)?;
        let (bytes, digest, revision) = artifacts::download(&root, &commit.output, &kind)?;
        artifact_response(bytes, digest, revision)
    })
    .await
}

pub(super) async fn evidence(
    State(app): State<App>,
    headers: HeaderMap,
    Path((id, commit, stage)): Path<(Uuid, Uuid, String)>,
) -> std::result::Result<Json<Value>, ApiError> {
    authorize(&app, &headers, false)?;
    if !["native", "step"].contains(&stage.as_str()) {
        return Err(CoreError::new("EVIDENCE_INVALID", "Select native or step evidence").into());
    }
    let repository = app.repository.clone();
    let root = app.root.clone();
    Ok(envelope(
        blocking(&app, move || {
            let commit = repository.get_commit(id, commit)?;
            let digest = if stage == "native" {
                commit.evidence.native_report_sha256
            } else {
                commit.evidence.step_report_sha256
            };
            let report = publication::read_report(&root, &digest)?;
            Ok(json!({"sha256":digest,"report":report}))
        })
        .await?,
    ))
}
