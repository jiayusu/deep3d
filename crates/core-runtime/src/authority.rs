//! Project-scoped goals, frozen plans and public verified commits.
//!
//! Evidence is constructed by the trusted service after actual artifact verification.
//! These types do not grant workers a publication API or verify geometry themselves.

use crate::repository::{commit, db_error, decoded, encoded, error, event};
use crate::*;
use core_domain::{Acceptance, PrecisionProfile, Result};
use core_storage::HeadToken;
use postgres::{GenericClient, IsolationLevel, Transaction};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::BTreeSet;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterProjectRequest {
    pub operation_id: Uuid,
    pub project: String,
    pub expected_head: HeadToken,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RequirementState {
    PendingConfirmation,
    Unverifiable,
    Conflicting,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UnresolvedRequirement {
    pub id: String,
    pub description: String,
    pub status: RequirementState,
    pub required: bool,
    pub source_refs: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GoalDefinition {
    pub schema_version: String,
    pub original_request: String,
    pub source_refs: Vec<String>,
    pub acceptance: Acceptance,
    pub precision: PrecisionProfile,
    pub unresolved: Vec<UnresolvedRequirement>,
}

impl GoalDefinition {
    pub fn validate(&self) -> Result<()> {
        fn refs(values: &[String]) -> bool {
            !values.is_empty()
                && values.len() <= 64
                && values.iter().all(|value| {
                    !value.trim().is_empty()
                        && value.chars().count() <= 2048
                        && !value.chars().any(char::is_control)
                })
        }
        let ids: BTreeSet<_> = self.unresolved.iter().map(|item| &item.id).collect();
        if self.schema_version != "deep3d.goal/1"
            || self.original_request.trim().is_empty()
            || self.original_request.chars().count() > 16384
            || !refs(&self.source_refs)
            || self.unresolved.len() > 128
            || ids.len() != self.unresolved.len()
            || self.unresolved.iter().any(|item| {
                !valid_key(&item.id)
                    || item.description.trim().is_empty()
                    || item.description.chars().count() > 4096
                    || !refs(&item.source_refs)
            })
        {
            return Err(error(
                "GOAL_INVALID",
                "A goal requires bounded intent and requirement provenance",
            ));
        }
        match &self.acceptance {
            Acceptance::Flange(value) => value.validate()?,
            Acceptance::Mechanical(value) => value.validate()?,
        }
        self.precision.validate()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateGoalRequest {
    pub operation_id: Uuid,
    pub expected_goal: Option<Uuid>,
    #[serde(with = "core_domain::u64_string")]
    pub expected_generation: u64,
    pub definition: GoalDefinition,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GoalRevision {
    pub revision_id: Uuid,
    pub project_id: Uuid,
    pub parent: Option<Uuid>,
    #[serde(with = "core_domain::u64_string")]
    pub generation: u64,
    pub definition: GoalDefinition,
    pub definition_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunBinding {
    pub goal_revision_id: Uuid,
    pub expected_public_commit: Option<Uuid>,
    #[serde(with = "core_domain::u64_string")]
    pub expected_public_generation: u64,
    pub publish_task: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanRevision {
    pub plan_revision_id: Uuid,
    pub project_id: Uuid,
    pub goal_revision_id: Uuid,
    pub binding: RunBinding,
    pub source: ArtifactSource,
    pub tasks: Vec<TaskSpec>,
    pub definition_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicationEvidence {
    pub schema_version: String,
    pub goal_revision_id: Uuid,
    pub acceptance_sha256: String,
    pub precision_sha256: String,
    pub source_sha256: String,
    pub materialization: String,
    pub native_report_sha256: String,
    pub step_report_sha256: String,
    pub native_geometry_sha256: String,
    pub step_geometry_sha256: String,
    pub assembly_sha256: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifiedCommit {
    pub commit_id: Uuid,
    pub project_id: Uuid,
    pub parent: Option<Uuid>,
    pub goal_revision_id: Uuid,
    pub plan_revision_id: Uuid,
    pub run_id: Uuid,
    pub task_key: String,
    pub attempt_id: Uuid,
    #[serde(with = "core_domain::u64_string")]
    pub goal_generation: u64,
    #[serde(with = "core_domain::u64_string")]
    pub public_generation: u64,
    pub output: TaskOutput,
    pub evidence: PublicationEvidence,
    pub created_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectView {
    pub project_id: Uuid,
    pub initial_source: ArtifactSource,
    pub goal_head: Option<Uuid>,
    #[serde(with = "core_domain::u64_string")]
    pub goal_generation: u64,
    pub public_head: Option<Uuid>,
    #[serde(with = "core_domain::u64_string")]
    pub public_generation: u64,
    pub goal: Option<GoalRevision>,
    pub public_commit: Option<VerifiedCommit>,
}

pub(crate) fn project_lock(transaction: &mut Transaction<'_>, project_id: Uuid) -> Result<()> {
    // Advisory project lock also serializes initial registration, before a row exists.
    transaction
        .query_one(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text,7345302439532))",
            &[&project_id.to_string()],
        )
        .map_err(db_error)?;
    transaction
        .query_opt(
            "SELECT project_id FROM projects WHERE project_id=$1 FOR UPDATE",
            &[&project_id],
        )
        .map_err(db_error)?;
    Ok(())
}

pub(crate) fn lock_project_for_run(transaction: &mut Transaction<'_>, run_id: Uuid) -> Result<()> {
    if let Some(row) = transaction
        .query_opt("SELECT spec FROM runs WHERE run_id=$1", &[&run_id])
        .map_err(db_error)?
    {
        let spec: RunSpec = decoded(row.get(0))?;
        if spec.authority.is_some() {
            project_lock(transaction, spec.project_id)?;
        }
    }
    Ok(())
}

fn goal(
    database: &mut impl GenericClient,
    project_id: Uuid,
    revision_id: Uuid,
) -> Result<GoalRevision> {
    let row = database
        .query_opt(
            "SELECT record FROM goal_revisions WHERE project_id=$1 AND revision_id=$2",
            &[&project_id, &revision_id],
        )
        .map_err(db_error)?
        .ok_or_else(|| error("GOAL_NOT_FOUND", "Unknown goal in this project"))?;
    decoded(row.get(0))
}

fn verified_commit(
    database: &mut impl GenericClient,
    project_id: Uuid,
    id: Uuid,
) -> Result<VerifiedCommit> {
    let row = database
        .query_opt(
            "SELECT record FROM verified_commits WHERE project_id=$1 AND commit_id=$2",
            &[&project_id, &id],
        )
        .map_err(db_error)?
        .ok_or_else(|| {
            error(
                "COMMIT_NOT_FOUND",
                "Unknown verified commit in this project",
            )
        })?;
    decoded(row.get(0))
}

fn project_view(database: &mut impl GenericClient, project_id: Uuid) -> Result<ProjectView> {
    let row = database
        .query_opt("SELECT * FROM projects WHERE project_id=$1", &[&project_id])
        .map_err(db_error)?
        .ok_or_else(|| error("PROJECT_NOT_FOUND", "Unknown registered project"))?;
    let goal_head: Option<Uuid> = row.get("goal_head");
    let public_head: Option<Uuid> = row.get("public_head");
    Ok(ProjectView {
        project_id,
        initial_source: decoded(row.get("initial_source"))?,
        goal_head,
        goal_generation: row.get::<_, i64>("goal_generation") as u64,
        public_head,
        public_generation: row.get::<_, i64>("public_generation") as u64,
        goal: goal_head
            .map(|id| goal(database, project_id, id))
            .transpose()?,
        public_commit: public_head
            .map(|id| verified_commit(database, project_id, id))
            .transpose()?,
    })
}

fn receipt<T: for<'a> Deserialize<'a>>(
    transaction: &mut Transaction<'_>,
    project_id: Uuid,
    operation_id: Uuid,
    digest: &str,
    kind: &str,
) -> Result<Option<T>> {
    if let Some(row) = transaction
        .query_opt(
            "SELECT request_digest,kind,result FROM authority_receipts WHERE project_id=$1 AND operation_id=$2",
            &[&project_id, &operation_id],
        )
        .map_err(db_error)?
    {
        if row.get::<_, String>(0) != digest || row.get::<_, String>(1) != kind {
            return Err(error("IDEMPOTENCY_CONFLICT", "Project operation identity was reused with other content"));
        }
        return decoded(row.get(2)).map(Some);
    }
    Ok(None)
}

fn store_receipt(
    transaction: &mut Transaction<'_>,
    project_id: Uuid,
    operation_id: Uuid,
    digest: &str,
    kind: &str,
    result: &impl Serialize,
) -> Result<()> {
    transaction
        .execute(
            "INSERT INTO authority_receipts(project_id,operation_id,request_digest,kind,result) VALUES($1,$2,$3,$4,$5)",
            &[&project_id, &operation_id, &digest, &kind, &encoded(result)?],
        )
        .map_err(db_error)?;
    Ok(())
}

fn next_generation(current: u64) -> Result<(u64, i64)> {
    let value = current
        .checked_add(1)
        .and_then(|v| i64::try_from(v).ok())
        .ok_or_else(|| error("EPOCH_EXHAUSTED", "Authority generation cannot wrap"))?;
    Ok((value as u64, value))
}

impl Repository {
    pub fn register_project(
        &self,
        project_id: Uuid,
        source: &ArtifactSource,
        operation_id: Uuid,
    ) -> Result<ProjectView> {
        if project_id.is_nil()
            || operation_id.is_nil()
            || source.head.revision_id.is_nil()
            || !valid_hash(&source.sha256)
            || source.path.is_empty()
            || source.path.len() > 512
        {
            return Err(error(
                "PROJECT_INVALID",
                "Register a frozen native source and stable operation identity",
            ));
        }
        let digest = request_digest(&(project_id, source, operation_id))?;
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        project_lock(&mut transaction, project_id)?;
        if let Some(result) = receipt(
            &mut transaction,
            project_id,
            operation_id,
            &digest,
            "register",
        )? {
            commit(transaction)?;
            return Ok(result);
        }
        if let Some(row) = transaction
            .query_opt(
                "SELECT initial_source FROM projects WHERE project_id=$1",
                &[&project_id],
            )
            .map_err(db_error)?
        {
            if decoded::<ArtifactSource>(row.get(0))? != *source {
                return Err(error(
                    "PROJECT_CONFLICT",
                    "This project was registered with another initial source",
                ));
            }
        } else {
            transaction
                .execute(
                    "INSERT INTO projects(project_id,initial_source) VALUES($1,$2)",
                    &[&project_id, &encoded(source)?],
                )
                .map_err(db_error)?;
        }
        let result = project_view(&mut transaction, project_id)?;
        store_receipt(
            &mut transaction,
            project_id,
            operation_id,
            &digest,
            "register",
            &result,
        )?;
        commit(transaction)?;
        Ok(result)
    }

    pub fn update_goal(
        &self,
        project_id: Uuid,
        request: &UpdateGoalRequest,
    ) -> Result<GoalRevision> {
        request.definition.validate()?;
        if project_id.is_nil()
            || request.operation_id.is_nil()
            || request.expected_goal.is_some_and(|id| id.is_nil())
        {
            return Err(error(
                "GOAL_INVALID",
                "Use project-scoped nonzero operation and goal identities",
            ));
        }
        let digest = request_digest(request)?;
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        project_lock(&mut transaction, project_id)?;
        if let Some(result) = receipt(
            &mut transaction,
            project_id,
            request.operation_id,
            &digest,
            "goal",
        )? {
            commit(transaction)?;
            return Ok(result);
        }
        let current = project_view(&mut transaction, project_id)?;
        if current.goal_head != request.expected_goal
            || current.goal_generation != request.expected_generation
        {
            return Err(error(
                "GOAL_CONFLICT",
                "Goal head or generation no longer matches",
            ));
        }
        let (generation, db_generation) = next_generation(current.goal_generation)?;
        let result = GoalRevision {
            revision_id: request.operation_id,
            project_id,
            parent: current.goal_head,
            generation,
            definition: request.definition.clone(),
            definition_sha256: request_digest(&request.definition)?,
        };
        transaction.execute("INSERT INTO goal_revisions(project_id,revision_id,parent,generation,record) VALUES($1,$2,$3,$4,$5)",
            &[&project_id,&result.revision_id,&result.parent,&db_generation,&encoded(&result)?]).map_err(db_error)?;
        transaction
            .execute(
                "UPDATE projects SET goal_head=$2,goal_generation=$3 WHERE project_id=$1",
                &[&project_id, &result.revision_id, &db_generation],
            )
            .map_err(db_error)?;
        // The project lock is always acquired before any authority-bound Run lock.
        let old_runs = transaction.query(
            "SELECT r.run_id,r.control_epoch FROM runs r JOIN plan_revisions p ON p.run_id=r.run_id
             WHERE p.project_id=$1 AND r.state IN ('running','paused') ORDER BY r.run_id FOR UPDATE OF r",
            &[&project_id],
        ).map_err(db_error)?;
        for run in old_runs {
            let run_id: Uuid = run.get(0);
            let epoch = run
                .get::<_, i64>(1)
                .checked_add(1)
                .ok_or_else(|| error("EPOCH_EXHAUSTED", "Control epoch cannot wrap"))?;
            transaction
                .execute(
                    "UPDATE runs SET state='cancelled',control_epoch=$2 WHERE run_id=$1",
                    &[&run_id, &epoch],
                )
                .map_err(db_error)?;
            transaction.execute("UPDATE attempts SET state='abandoned',error='GOAL_CHANGED',finished_at=clock_timestamp() WHERE run_id=$1 AND state='running'", &[&run_id]).map_err(db_error)?;
            transaction.execute("UPDATE tasks SET state='cancelled',error='GOAL_CHANGED' WHERE run_id=$1 AND state IN ('pending','retry_wait','running')", &[&run_id]).map_err(db_error)?;
            event(
                &mut transaction,
                run_id,
                "goal.changed",
                json!({"goal_revision_id":result.revision_id,"control_epoch":epoch}),
            )?;
        }
        store_receipt(
            &mut transaction,
            project_id,
            request.operation_id,
            &digest,
            "goal",
            &result,
        )?;
        commit(transaction)?;
        Ok(result)
    }

    pub fn get_project(&self, project_id: Uuid) -> Result<ProjectView> {
        let mut client = self.connect()?;
        let mut transaction = client
            .build_transaction()
            .isolation_level(IsolationLevel::RepeatableRead)
            .read_only(true)
            .start()
            .map_err(db_error)?;
        let result = project_view(&mut transaction, project_id)?;
        commit(transaction)?;
        Ok(result)
    }

    pub fn get_goal(&self, project_id: Uuid, revision_id: Uuid) -> Result<GoalRevision> {
        goal(&mut self.connect()?, project_id, revision_id)
    }

    pub fn get_commit(&self, project_id: Uuid, commit_id: Uuid) -> Result<VerifiedCommit> {
        verified_commit(&mut self.connect()?, project_id, commit_id)
    }

    pub fn get_plan(&self, project_id: Uuid, run_id: Uuid) -> Result<PlanRevision> {
        let mut client = self.connect()?;
        let row = client
            .query_opt(
                "SELECT record FROM plan_revisions WHERE project_id=$1 AND run_id=$2",
                &[&project_id, &run_id],
            )
            .map_err(db_error)?
            .ok_or_else(|| error("PLAN_NOT_FOUND", "Unknown plan in this project"))?;
        decoded(row.get(0))
    }
}

pub(crate) fn prepare_plan(
    transaction: &mut Transaction<'_>,
    spec: &RunSpec,
) -> Result<Option<PlanRevision>> {
    let Some(binding) = &spec.authority else {
        return Ok(None);
    };
    project_lock(transaction, spec.project_id)?;
    let current = project_view(transaction, spec.project_id)?;
    if current.goal_head != Some(binding.goal_revision_id) {
        return Err(error(
            "STALE_GOAL",
            "The run does not bind the current goal",
        ));
    }
    if current.public_head != binding.expected_public_commit
        || current.public_generation != binding.expected_public_generation
    {
        return Err(error(
            "PUBLIC_HEAD_CONFLICT",
            "Public base no longer matches",
        ));
    }
    let source = current
        .public_commit
        .as_ref()
        .map(|value| &value.output.source)
        .unwrap_or(&current.initial_source);
    if source != &spec.source {
        return Err(error(
            "PROJECT_CONFLICT",
            "Plan source is not the exact current public or initial source",
        ));
    }
    let goal = current
        .goal
        .ok_or_else(|| error("GOAL_NOT_FOUND", "No accepted project goal"))?;
    if goal
        .definition
        .unresolved
        .iter()
        .any(|value| value.required)
    {
        return Err(error(
            "UNRESOLVED_GOAL",
            "Required unresolved conditions prevent goal execution/publication",
        ));
    }
    Ok(Some(PlanRevision {
        plan_revision_id: spec.run_id,
        project_id: spec.project_id,
        goal_revision_id: binding.goal_revision_id,
        binding: binding.clone(),
        source: spec.source.clone(),
        tasks: spec.tasks.clone(),
        definition_sha256: request_digest(&(binding, &spec.source, &spec.tasks))?,
    }))
}

pub(crate) fn insert_plan(transaction: &mut Transaction<'_>, plan: &PlanRevision) -> Result<()> {
    transaction.execute("INSERT INTO plan_revisions(project_id,run_id,goal_revision_id,record) VALUES($1,$2,$3,$4)",
        &[&plan.project_id,&plan.plan_revision_id,&plan.goal_revision_id,&encoded(plan)?]).map_err(db_error)?;
    Ok(())
}

pub(crate) fn publish(
    transaction: &mut Transaction<'_>,
    spec: &RunSpec,
    token: &LeaseToken,
    output: &TaskOutput,
    evidence: Option<&PublicationEvidence>,
) -> Result<Option<VerifiedCommit>> {
    let Some(binding) = &spec.authority else {
        if evidence.is_some() {
            return Err(error(
                "PUBLICATION_INVALID",
                "Private runs cannot supply publication evidence",
            ));
        }
        return Ok(None);
    };
    let current = project_view(transaction, spec.project_id)?;
    if current.goal_head != Some(binding.goal_revision_id) {
        return Err(error(
            "STALE_GOAL",
            "Execution no longer belongs to the current goal",
        ));
    }
    if token.task_key != binding.publish_task {
        if evidence.is_some() {
            return Err(error(
                "PUBLICATION_INVALID",
                "Only the frozen publish task can publish",
            ));
        }
        return Ok(None);
    }
    let evidence = evidence.ok_or_else(|| {
        error(
            "PUBLICATION_EVIDENCE_REQUIRED",
            "Public commits require independently verified artifact evidence",
        )
    })?;
    let remaining: i64 = transaction
        .query_one(
            "SELECT count(*) FROM tasks WHERE run_id=$1 AND task_key<>$2 AND state<>'succeeded'",
            &[&spec.run_id, &token.task_key],
        )
        .map_err(db_error)?
        .get(0);
    if remaining != 0 {
        return Err(error(
            "PUBLICATION_INVALID",
            "The public task must follow all required plan tasks",
        ));
    }
    if current.public_head != binding.expected_public_commit
        || current.public_generation != binding.expected_public_generation
    {
        return Err(error(
            "PUBLIC_HEAD_CONFLICT",
            "Another verified commit changed the public base",
        ));
    }
    let goal = current
        .goal
        .ok_or_else(|| error("GOAL_NOT_FOUND", "No accepted goal"))?;
    if goal.definition.unresolved.iter().any(|item| item.required) {
        return Err(error(
            "UNRESOLVED_GOAL",
            "Required unresolved conditions prevent publication",
        ));
    }
    let hashes = [
        &evidence.acceptance_sha256,
        &evidence.precision_sha256,
        &evidence.source_sha256,
        &evidence.materialization,
        &evidence.native_report_sha256,
        &evidence.step_report_sha256,
        &evidence.native_geometry_sha256,
        &evidence.step_geometry_sha256,
    ];
    if evidence.schema_version != "deep3d.publication-evidence/1"
        || evidence.goal_revision_id != binding.goal_revision_id
        || evidence.acceptance_sha256 != request_digest(&goal.definition.acceptance)?
        || evidence.precision_sha256 != request_digest(&goal.definition.precision)?
        || evidence.source_sha256 != output.source.sha256
        || evidence.materialization != output.materialization
        || evidence.step_geometry_sha256 != output.step_sha256
        || hashes.iter().any(|value| !valid_hash(value))
        || evidence
            .assembly_sha256
            .as_ref()
            .is_some_and(|value| !valid_hash(value))
        || matches!(goal.definition.acceptance, Acceptance::Mechanical(_))
            && evidence.assembly_sha256.is_none()
    {
        return Err(error(
            "PUBLICATION_INVALID",
            "Evidence does not bind this goal, source, materialization and artifact set",
        ));
    }
    let (generation, db_generation) = next_generation(current.public_generation)?;
    let created_unix_ms = transaction
        .query_one(
            "SELECT (extract(epoch FROM clock_timestamp())*1000)::bigint",
            &[],
        )
        .map_err(db_error)?
        .get(0);
    let result = VerifiedCommit {
        commit_id: token.attempt_id,
        project_id: spec.project_id,
        parent: current.public_head,
        goal_revision_id: binding.goal_revision_id,
        plan_revision_id: spec.run_id,
        run_id: spec.run_id,
        task_key: token.task_key.clone(),
        attempt_id: token.attempt_id,
        goal_generation: current.goal_generation,
        public_generation: generation,
        output: output.clone(),
        evidence: evidence.clone(),
        created_unix_ms,
    };
    transaction.execute("INSERT INTO verified_commits(project_id,commit_id,parent,goal_revision_id,run_id,task_key,attempt_id,public_generation,record) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        &[&result.project_id,&result.commit_id,&result.parent,&result.goal_revision_id,&result.run_id,&result.task_key,&result.attempt_id,&db_generation,&encoded(&result)?]).map_err(db_error)?;
    transaction
        .execute(
            "UPDATE projects SET public_head=$2,public_generation=$3 WHERE project_id=$1",
            &[&spec.project_id, &result.commit_id, &db_generation],
        )
        .map_err(db_error)?;
    event(
        transaction,
        spec.run_id,
        "project.published",
        json!({"project_id":spec.project_id,"commit_id":result.commit_id,"goal_revision_id":result.goal_revision_id,"public_generation":generation.to_string()}),
    )?;
    Ok(Some(result))
}
