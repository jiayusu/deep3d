use crate::*;
use core_domain::{digest_bytes, CoreError, Result};
use postgres::{Client, Config, GenericClient, IsolationLevel, NoTls, Row, Transaction};
use serde::{de::DeserializeOwned, Serialize};
use serde_json::{json, Value};
use std::{str::FromStr, time::Duration};
use uuid::Uuid;

const MIGRATION: &str = include_str!("../../../migrations/0001_durable_runtime.sql");

#[derive(Clone)]
pub struct Repository {
    config: Config,
    lease_seconds: i32,
}

fn error(code: &str, message: &str) -> CoreError {
    CoreError::new(code, message)
}

fn db_error(cause: postgres::Error) -> CoreError {
    match cause.code().map(|code| code.code()) {
        Some("55P03" | "57014" | "40P01" | "40001") => error(
            "DATABASE_BUSY",
            "Database transaction must be retried with the same identity",
        ),
        Some("42P01" | "3F000") => {
            error("DATABASE_NOT_INITIALIZED", "Apply runtime migrations first")
        }
        _ => error("DATABASE_UNAVAILABLE", "Runtime database operation failed"),
    }
}

fn encoded(value: &impl Serialize) -> Result<Value> {
    serde_json::to_value(value)
        .map_err(|_| error("RUNTIME_ENCODING", "Cannot encode runtime contract"))
}

fn decoded<T: DeserializeOwned>(value: Value) -> Result<T> {
    serde_json::from_value(value)
        .map_err(|_| error("RUNTIME_CORRUPT", "Stored runtime contract is invalid"))
}

fn commit(transaction: Transaction<'_>) -> Result<()> {
    transaction.commit().map_err(|_| {
        error(
            "COMMIT_OUTCOME_UNKNOWN",
            "Commit response unavailable; retry the same operation identity",
        )
    })
}

fn run_lock(transaction: &mut Transaction<'_>, run_id: Uuid) -> Result<Row> {
    transaction
        .query_opt("SELECT * FROM runs WHERE run_id=$1 FOR UPDATE", &[&run_id])
        .map_err(db_error)?
        .ok_or_else(|| error("RUN_NOT_FOUND", "Unknown run"))
}

fn event(
    transaction: &mut Transaction<'_>,
    run_id: Uuid,
    kind: &str,
    payload: Value,
) -> Result<()> {
    let seq: i64 = transaction
        .query_one(
            "UPDATE runs SET event_seq=event_seq+1 WHERE run_id=$1 RETURNING event_seq",
            &[&run_id],
        )
        .map_err(db_error)?
        .get(0);
    transaction
        .execute(
            "INSERT INTO events(run_id,seq,event_id,kind,payload) VALUES($1,$2,$3,$4,$5)",
            &[&run_id, &seq, &Uuid::new_v4(), &kind, &payload],
        )
        .map_err(db_error)?;
    Ok(())
}

fn view(database: &mut impl GenericClient, run_id: Uuid) -> Result<RunView> {
    let row = database
        .query_opt("SELECT * FROM runs WHERE run_id=$1", &[&run_id])
        .map_err(db_error)?
        .ok_or_else(|| error("RUN_NOT_FOUND", "Unknown run"))?;
    let tasks = database
        .query(
            "SELECT * FROM tasks WHERE run_id=$1 ORDER BY task_key",
            &[&run_id],
        )
        .map_err(db_error)?
        .into_iter()
        .map(|row| {
            Ok(TaskView {
                spec: decoded(row.get("spec"))?,
                state: row.get("state"),
                execution_epoch: row.get("execution_epoch"),
                output: row
                    .get::<_, Option<Value>>("output")
                    .map(decoded)
                    .transpose()?,
                error: row.get("error"),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let attempts = database.query(
        "SELECT task_key,attempt_id,worker_id,execution_epoch,control_epoch,state,error,source,
         (extract(epoch FROM started_at)*1000)::bigint AS started,
         (extract(epoch FROM lease_until)*1000)::bigint AS lease_until_ms,
         (extract(epoch FROM deadline)*1000)::bigint AS deadline_ms
         FROM attempts WHERE run_id=$1 ORDER BY started_at,task_key,execution_epoch", &[&run_id],
    ).map_err(db_error)?.into_iter().map(|row| json!({
        "task_key": row.get::<_, String>("task_key"),
        "attempt_id": row.get::<_, Uuid>("attempt_id"),
        "worker_id": row.get::<_, String>("worker_id"),
        "execution_epoch": row.get::<_, i64>("execution_epoch"),
        "control_epoch": row.get::<_, i64>("control_epoch"),
        "state": row.get::<_, String>("state"), "error": row.get::<_, Option<String>>("error"),
        "source": row.get::<_, Value>("source"), "started_unix_ms": row.get::<_, i64>("started"),
        "lease_until_unix_ms": row.get::<_, i64>("lease_until_ms"),
        "deadline_unix_ms": row.get::<_, i64>("deadline_ms"),
    })).collect();
    Ok(RunView {
        spec: decoded(row.get("spec"))?,
        state: row.get("state"),
        control_epoch: row.get("control_epoch"),
        event_seq: row.get("event_seq"),
        tasks,
        attempts,
    })
}

fn token_valid(token: &LeaseToken) -> Result<()> {
    if token.run_id.is_nil()
        || token.attempt_id.is_nil()
        || !valid_key(&token.task_key)
        || !valid_key(&token.worker_id)
        || token.execution_epoch < 1
        || token.control_epoch < 0
    {
        return Err(error("LEASE_INVALID", "Invalid execution lease identity"));
    }
    Ok(())
}

fn lease_from_row(row: &Row, lease_seconds: i32) -> Result<Lease> {
    let task: TaskSpec = decoded(row.get("task_spec"))?;
    Ok(Lease {
        token: LeaseToken {
            run_id: row.get("run_id"),
            task_key: row.get("task_key"),
            attempt_id: row.get("attempt_id"),
            worker_id: row.get("worker_id"),
            execution_epoch: row.get("execution_epoch"),
            control_epoch: row.get("control_epoch"),
        },
        source: decoded(row.get("source"))?,
        changes_mm: decoded(row.get("changes_mm"))?,
        acceptance: task.acceptance,
        lease_until_unix_ms: row.get("lease_ms"),
        deadline_unix_ms: row.get("deadline_ms"),
        heartbeat_after_seconds: f64::from(lease_seconds) / 3.0,
    })
}

fn read_lease(
    transaction: &mut Transaction<'_>,
    token: &LeaseToken,
    seconds: i32,
) -> Result<Lease> {
    let row = transaction
        .query_one(
            "SELECT a.*, t.spec AS task_spec,
         (extract(epoch FROM a.lease_until)*1000)::bigint AS lease_ms,
         (extract(epoch FROM a.deadline)*1000)::bigint AS deadline_ms FROM attempts a
         JOIN tasks t ON t.run_id=a.run_id AND t.task_key=a.task_key
         WHERE a.run_id=$1 AND a.task_key=$2 AND a.attempt_id=$3",
            &[&token.run_id, &token.task_key, &token.attempt_id],
        )
        .map_err(db_error)?;
    lease_from_row(&row, seconds)
}

fn lock_attempt(transaction: &mut Transaction<'_>, token: &LeaseToken) -> Result<(Row, Row, Row)> {
    token_valid(token)?;
    let run = run_lock(transaction, token.run_id)?;
    let task = transaction
        .query_opt(
            "SELECT * FROM tasks WHERE run_id=$1 AND task_key=$2 FOR UPDATE",
            &[&token.run_id, &token.task_key],
        )
        .map_err(db_error)?
        .ok_or_else(|| error("STALE_EXECUTION_LEASE", "Task is not in this run"))?;
    let attempt = transaction
        .query_opt(
            "SELECT * FROM attempts WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3 FOR UPDATE",
            &[&token.run_id, &token.task_key, &token.attempt_id],
        )
        .map_err(db_error)?
        .ok_or_else(|| error("STALE_EXECUTION_LEASE", "Unknown execution attempt"))?;
    if attempt.get::<_, String>("worker_id") != token.worker_id
        || attempt.get::<_, i64>("execution_epoch") != token.execution_epoch
        || attempt.get::<_, i64>("control_epoch") != token.control_epoch
    {
        return Err(error(
            "STALE_EXECUTION_LEASE",
            "Execution identity no longer matches",
        ));
    }
    Ok((run, task, attempt))
}

fn require_active(
    transaction: &mut Transaction<'_>,
    token: &LeaseToken,
    run: &Row,
    task: &Row,
    attempt: &Row,
) -> Result<()> {
    if run.get::<_, String>("state") != "running"
        || run.get::<_, i64>("control_epoch") != token.control_epoch
        || task.get::<_, String>("state") != "running"
        || task.get::<_, Option<Uuid>>("current_attempt_id") != Some(token.attempt_id)
        || task.get::<_, i64>("execution_epoch") != token.execution_epoch
        || attempt.get::<_, String>("state") != "running"
    {
        return Err(error(
            "STALE_EXECUTION_LEASE",
            "Execution authority has been revoked",
        ));
    }
    // This clock read happens only after all authority rows are locked.
    let alive: bool = transaction
        .query_one(
            "SELECT lease_until>clock_timestamp() AND deadline>clock_timestamp() FROM attempts
         WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3",
            &[&token.run_id, &token.task_key, &token.attempt_id],
        )
        .map_err(db_error)?
        .get(0);
    if !alive {
        return Err(error(
            "STALE_EXECUTION_LEASE",
            "Execution lease or deadline expired",
        ));
    }
    Ok(())
}

fn fail_run(
    transaction: &mut Transaction<'_>,
    run_id: Uuid,
    task_key: &str,
    code: &str,
) -> Result<()> {
    transaction
        .execute(
            "UPDATE runs SET state='failed',control_epoch=control_epoch+1 WHERE run_id=$1",
            &[&run_id],
        )
        .map_err(db_error)?;
    transaction
        .execute(
            "UPDATE attempts SET state='abandoned',error='RUN_FAILED',finished_at=clock_timestamp()
         WHERE run_id=$1 AND state='running'",
            &[&run_id],
        )
        .map_err(db_error)?;
    transaction
        .execute(
            "UPDATE tasks SET state='cancelled',error='RUN_FAILED' WHERE run_id=$1
         AND state IN ('pending','retry_wait','running') AND task_key<>$2",
            &[&run_id, &task_key],
        )
        .map_err(db_error)?;
    transaction
        .execute(
            "UPDATE tasks SET state='failed',error=$3 WHERE run_id=$1 AND task_key=$2",
            &[&run_id, &task_key, &code],
        )
        .map_err(db_error)?;
    event(
        transaction,
        run_id,
        "run.failed",
        json!({"task_key":task_key,"code":code}),
    )
}

fn retry_task(
    transaction: &mut Transaction<'_>,
    run_id: Uuid,
    key: &str,
    spec: &TaskSpec,
    epoch: i64,
    retryable: bool,
    code: &str,
) -> Result<()> {
    if retryable && epoch < i64::from(spec.max_attempts) {
        let delay = (1_i32 << (epoch.min(6) as u32 - 1)).min(60);
        transaction
            .execute(
                "UPDATE tasks SET state='retry_wait',error=$3,
             available_at=clock_timestamp()+$4::integer*interval '1 second'
             WHERE run_id=$1 AND task_key=$2",
                &[&run_id, &key, &code, &delay],
            )
            .map_err(db_error)?;
        event(
            transaction,
            run_id,
            "task.retry_wait",
            json!({"task_key":key,"code":code}),
        )
    } else {
        fail_run(transaction, run_id, key, code)
    }
}

impl Repository {
    pub fn new(database_url: &str, lease_seconds: i32) -> Result<Self> {
        if !(1..=300).contains(&lease_seconds) {
            return Err(error(
                "RUNTIME_CONFIG",
                "Lease duration must be 1 to 300 seconds",
            ));
        }
        let mut config = Config::from_str(database_url).map_err(|_| {
            error(
                "RUNTIME_CONFIG",
                "Invalid database connection configuration",
            )
        })?;
        config.connect_timeout(Duration::from_secs(3));
        Ok(Self {
            config,
            lease_seconds,
        })
    }

    fn connect(&self) -> Result<Client> {
        let mut client = self.config.connect(NoTls).map_err(db_error)?;
        client
            .batch_execute(
                "SET search_path=deep3d_runtime,pg_catalog;
            SET statement_timeout='5s'; SET lock_timeout='1500ms';
            SET idle_in_transaction_session_timeout='5s';",
            )
            .map_err(db_error)?;
        Ok(client)
    }

    pub fn migrate(&self) -> Result<()> {
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        transaction
            .query_one("SELECT pg_advisory_xact_lock(7345302439531)", &[])
            .map_err(db_error)?;
        transaction
            .batch_execute(
                "CREATE SCHEMA IF NOT EXISTS deep3d_runtime;
            CREATE TABLE IF NOT EXISTS deep3d_runtime.schema_migrations(
                version integer PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp());",
            )
            .map_err(db_error)?;
        // The migration identity is independent of Windows checkout line endings.
        let checksum = digest_bytes(MIGRATION.replace("\r\n", "\n").as_bytes());
        if let Some(row) = transaction
            .query_opt(
                "SELECT checksum FROM schema_migrations WHERE version=1",
                &[],
            )
            .map_err(db_error)?
        {
            if row.get::<_, String>(0) != checksum {
                return Err(error(
                    "MIGRATION_CONFLICT",
                    "Applied migration checksum does not match",
                ));
            }
        } else {
            transaction.batch_execute(MIGRATION).map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version,checksum) VALUES(1,$1)",
                    &[&checksum],
                )
                .map_err(db_error)?;
        }
        commit(transaction)
    }

    pub fn submit(&self, spec: &RunSpec) -> Result<RunView> {
        validate_spec(spec)?;
        let digest = request_digest(spec)?;
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        transaction
            .query_one(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                &[&format!("submit:{}", spec.run_id)],
            )
            .map_err(db_error)?;
        if let Some(row) = transaction
            .query_opt(
                "SELECT spec_sha256 FROM runs WHERE run_id=$1",
                &[&spec.run_id],
            )
            .map_err(db_error)?
        {
            if row.get::<_, String>(0) != digest {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Run identity was reused with another definition",
                ));
            }
            let result = view(&mut transaction, spec.run_id)?;
            commit(transaction)?;
            return Ok(result);
        }
        transaction.execute(
            "INSERT INTO runs(run_id,spec,spec_sha256,state,control_epoch) VALUES($1,$2,$3,'running',0)",
            &[&spec.run_id, &encoded(spec)?, &digest],
        ).map_err(db_error)?;
        for task in &spec.tasks {
            transaction
                .execute(
                    "INSERT INTO tasks(run_id,task_key,spec,state) VALUES($1,$2,$3,'pending')",
                    &[&spec.run_id, &task.key, &encoded(task)?],
                )
                .map_err(db_error)?;
        }
        for task in &spec.tasks {
            for dependency in &task.depends_on {
                transaction
                    .execute(
                        "INSERT INTO dependencies(run_id,task_key,dependency_key) VALUES($1,$2,$3)",
                        &[&spec.run_id, &task.key, &dependency],
                    )
                    .map_err(db_error)?;
            }
        }
        event(
            &mut transaction,
            spec.run_id,
            "run.started",
            json!({"tasks":spec.tasks.len()}),
        )?;
        let result = view(&mut transaction, spec.run_id)?;
        commit(transaction)?;
        Ok(result)
    }

    pub fn get_run(&self, run_id: Uuid) -> Result<RunView> {
        let mut client = self.connect()?;
        let mut transaction = client
            .build_transaction()
            .isolation_level(IsolationLevel::RepeatableRead)
            .read_only(true)
            .start()
            .map_err(db_error)?;
        let result = view(&mut transaction, run_id)?;
        commit(transaction)?;
        Ok(result)
    }

    pub fn claim(&self, request: &ClaimRequest) -> Result<Option<Lease>> {
        if request.claim_id.is_nil() || !valid_key(&request.worker_id) {
            return Err(error(
                "CLAIM_INVALID",
                "Claim and worker identities are required",
            ));
        }
        let digest = request_digest(request)?;
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        transaction
            .query_one(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                &[&format!("claim:{}:{}", request.worker_id, request.claim_id)],
            )
            .map_err(db_error)?;
        if let Some(row) = transaction
            .query_opt(
                "SELECT * FROM claim_receipts WHERE worker_id=$1 AND claim_id=$2",
                &[&request.worker_id, &request.claim_id],
            )
            .map_err(db_error)?
        {
            if row.get::<_, String>("request_digest") != digest {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Claim identity was reused with another request",
                ));
            }
            if let Some(run_id) = row.get::<_, Option<Uuid>>("run_id") {
                let attempt_id: Uuid = row.get("attempt_id");
                let key: String = row.get("task_key");
                let original = transaction
                    .query_one(
                        "SELECT * FROM attempts WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3",
                        &[&run_id, &key, &attempt_id],
                    )
                    .map_err(db_error)?;
                let token = LeaseToken {
                    run_id,
                    task_key: key,
                    attempt_id,
                    worker_id: request.worker_id.clone(),
                    execution_epoch: original.get("execution_epoch"),
                    control_epoch: original.get("control_epoch"),
                };
                let (run, task, attempt) = lock_attempt(&mut transaction, &token)?;
                require_active(&mut transaction, &token, &run, &task, &attempt)?;
                let result = read_lease(&mut transaction, &token, self.lease_seconds)?;
                commit(transaction)?;
                return Ok(Some(result));
            }
            commit(transaction)?;
            return Ok(None);
        }
        let chosen = transaction
            .query_opt(
                "SELECT r.run_id FROM runs r WHERE r.state='running'
             AND ($1::uuid IS NULL OR r.run_id=$1)
             AND (SELECT count(*) FROM tasks a WHERE a.run_id=r.run_id AND a.state='running')<8
             AND EXISTS(SELECT 1 FROM tasks t WHERE t.run_id=r.run_id
               AND t.state IN ('pending','retry_wait') AND t.available_at<=clock_timestamp()
               AND t.execution_epoch<(t.spec->>'max_attempts')::int
               AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks p
                 ON p.run_id=d.run_id AND p.task_key=d.dependency_key
                 WHERE d.run_id=t.run_id AND d.task_key=t.task_key AND p.state<>'succeeded'))
             ORDER BY r.last_dispatch_at NULLS FIRST,r.created_at,r.run_id
             FOR UPDATE OF r SKIP LOCKED LIMIT 1",
                &[&request.run_id],
            )
            .map_err(db_error)?;
        let Some(chosen) = chosen else {
            transaction.execute("INSERT INTO claim_receipts(worker_id,claim_id,request_digest) VALUES($1,$2,$3)",
                                &[&request.worker_id,&request.claim_id,&digest]).map_err(db_error)?;
            commit(transaction)?;
            return Ok(None);
        };
        let run_id: Uuid = chosen.get(0);
        let run = transaction
            .query_one("SELECT * FROM runs WHERE run_id=$1", &[&run_id])
            .map_err(db_error)?;
        let task = transaction.query_one(
            "SELECT t.* FROM tasks t WHERE t.run_id=$1 AND t.state IN ('pending','retry_wait')
             AND t.available_at<=clock_timestamp() AND t.execution_epoch<(t.spec->>'max_attempts')::int
             AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks p
               ON p.run_id=d.run_id AND p.task_key=d.dependency_key
               WHERE d.run_id=t.run_id AND d.task_key=t.task_key AND p.state<>'succeeded')
             ORDER BY t.available_at,t.task_key FOR UPDATE OF t LIMIT 1", &[&run_id],
        ).map_err(db_error)?;
        let task_spec: TaskSpec = decoded(task.get("spec"))?;
        let run_spec: RunSpec = decoded(run.get("spec"))?;
        let source = if let Some(source_key) = &task_spec.source_task {
            let parent: Value = transaction
                .query_one(
                    "SELECT output FROM tasks WHERE run_id=$1 AND task_key=$2",
                    &[&run_id, &source_key],
                )
                .map_err(db_error)?
                .get(0);
            decoded::<TaskOutput>(parent)?.source
        } else {
            run_spec.source
        };
        let epoch = task
            .get::<_, i64>("execution_epoch")
            .checked_add(1)
            .ok_or_else(|| error("EPOCH_EXHAUSTED", "Execution epoch cannot wrap"))?;
        let token = LeaseToken {
            run_id,
            task_key: task_spec.key.clone(),
            attempt_id: Uuid::new_v4(),
            worker_id: request.worker_id.clone(),
            execution_epoch: epoch,
            control_epoch: run.get("control_epoch"),
        };
        transaction.execute(
            "INSERT INTO attempts(run_id,task_key,attempt_id,worker_id,execution_epoch,control_epoch,
               source,changes_mm,state,lease_until,deadline)
             VALUES($1,$2,$3,$4,$5,$6,$7,$8,'running',
               clock_timestamp()+LEAST($9::integer,$10::integer)*interval '1 second',clock_timestamp()+$10::integer*interval '1 second')",
            &[&run_id,&token.task_key,&token.attempt_id,&token.worker_id,&epoch,&token.control_epoch,
                &encoded(&source)?,&encoded(&task_spec.changes_mm)?,&self.lease_seconds,&task_spec.timeout_seconds],
        ).map_err(db_error)?;
        transaction.execute("UPDATE tasks SET state='running',execution_epoch=$3,current_attempt_id=$4,error=NULL WHERE run_id=$1 AND task_key=$2",
                            &[&run_id,&token.task_key,&epoch,&token.attempt_id]).map_err(db_error)?;
        transaction
            .execute(
                "UPDATE runs SET last_dispatch_at=clock_timestamp() WHERE run_id=$1",
                &[&run_id],
            )
            .map_err(db_error)?;
        transaction.execute("INSERT INTO claim_receipts(worker_id,claim_id,request_digest,run_id,task_key,attempt_id) VALUES($1,$2,$3,$4,$5,$6)",
                            &[&request.worker_id,&request.claim_id,&digest,&run_id,&token.task_key,&token.attempt_id]).map_err(db_error)?;
        event(
            &mut transaction,
            run_id,
            "task.claimed",
            json!({"task_key":token.task_key,"attempt_id":token.attempt_id,
            "worker_id":token.worker_id,"execution_epoch":epoch}),
        )?;
        let lease = read_lease(&mut transaction, &token, self.lease_seconds)?;
        commit(transaction)?;
        Ok(Some(lease))
    }

    pub fn heartbeat(&self, token: &LeaseToken) -> Result<Lease> {
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        let (run, task, attempt) = lock_attempt(&mut transaction, token)?;
        require_active(&mut transaction, token, &run, &task, &attempt)?;
        transaction.execute("UPDATE attempts SET lease_until=LEAST(deadline,clock_timestamp()+$4::integer*interval '1 second')
            WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3", &[&token.run_id,&token.task_key,&token.attempt_id,&self.lease_seconds]).map_err(db_error)?;
        let lease = read_lease(&mut transaction, token, self.lease_seconds)?;
        commit(transaction)?;
        Ok(lease)
    }

    pub fn source_for(&self, token: &LeaseToken) -> Result<ArtifactSource> {
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        let (run, task, attempt) = lock_attempt(&mut transaction, token)?;
        require_active(&mut transaction, token, &run, &task, &attempt)?;
        let source = decoded(attempt.get("source"))?;
        commit(transaction)?;
        Ok(source)
    }

    pub fn completion(&self, token: &LeaseToken, digest: &str) -> Result<Option<TaskOutput>> {
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        let (run, task, attempt) = lock_attempt(&mut transaction, token)?;
        let existing: Option<String> = attempt.get("completion_digest");
        if let Some(previous) = existing {
            if previous != digest {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Completion identity was reused with other content",
                ));
            }
            let output = decoded(attempt.get("output"))?;
            commit(transaction)?;
            return Ok(Some(output));
        }
        require_active(&mut transaction, token, &run, &task, &attempt)?;
        commit(transaction)?;
        Ok(None)
    }

    pub fn complete(
        &self,
        token: &LeaseToken,
        output: &TaskOutput,
        digest: &str,
    ) -> Result<TaskOutput> {
        if !valid_hash(digest)
            || output.schema_version != "deep3d.task-output/1"
            || !valid_hash(&output.source.sha256)
            || !valid_hash(&output.materialization)
            || !valid_hash(&output.step_sha256)
        {
            return Err(error(
                "OUTPUT_INVALID",
                "Invalid verified task output contract",
            ));
        }
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        let (run, task, attempt) = lock_attempt(&mut transaction, token)?;
        if let Some(previous) = attempt.get::<_, Option<String>>("completion_digest") {
            let original: TaskOutput = decoded(attempt.get("output"))?;
            if previous != digest || original != *output {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Completion identity was reused with other content",
                ));
            }
            commit(transaction)?;
            return Ok(original);
        }
        require_active(&mut transaction, token, &run, &task, &attempt)?;
        let spec: RunSpec = decoded(run.get("spec"))?;
        let source: ArtifactSource = decoded(attempt.get("source"))?;
        if output.project_id != spec.project_id
            || output.source.head.generation
                != source
                    .head
                    .generation
                    .checked_add(1)
                    .ok_or_else(|| error("EPOCH_EXHAUSTED", "Output generation cannot wrap"))?
        {
            return Err(error(
                "OUTPUT_INVALID",
                "Output belongs to another project or generation",
            ));
        }
        let output_json = encoded(output)?;
        transaction.execute("UPDATE attempts SET state='succeeded',output=$4,completion_digest=$5,finished_at=clock_timestamp()
            WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3", &[&token.run_id,&token.task_key,&token.attempt_id,&output_json,&digest]).map_err(db_error)?;
        transaction.execute("UPDATE tasks SET state='succeeded',output=$3,error=NULL WHERE run_id=$1 AND task_key=$2",
                            &[&token.run_id,&token.task_key,&output_json]).map_err(db_error)?;
        event(
            &mut transaction,
            token.run_id,
            "task.succeeded",
            json!({"task_key":token.task_key,"attempt_id":token.attempt_id,
            "native_sha256":output.source.sha256,"materialization":output.materialization}),
        )?;
        let remaining: i64 = transaction
            .query_one(
                "SELECT count(*) FROM tasks WHERE run_id=$1 AND state<>'succeeded'",
                &[&token.run_id],
            )
            .map_err(db_error)?
            .get(0);
        if remaining == 0 {
            transaction.execute("UPDATE runs SET state='completed',control_epoch=control_epoch+1 WHERE run_id=$1",&[&token.run_id]).map_err(db_error)?;
            event(&mut transaction, token.run_id, "run.completed", json!({}))?;
        }
        commit(transaction)?;
        Ok(output.clone())
    }

    pub fn fail(&self, request: &FailureRequest) -> Result<()> {
        if !valid_key(&request.code) {
            return Err(error(
                "FAILURE_INVALID",
                "Use a bounded structured failure code",
            ));
        }
        let digest = request_digest(request)?;
        let token = &request.lease;
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        let (run, task, attempt) = lock_attempt(&mut transaction, token)?;
        if let Some(previous) = attempt.get::<_, Option<String>>("failure_digest") {
            if previous != digest {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Failure identity was reused with other content",
                ));
            }
            commit(transaction)?;
            return Ok(());
        }
        require_active(&mut transaction, token, &run, &task, &attempt)?;
        transaction.execute("UPDATE attempts SET state='failed',error=$4,failure_digest=$5,finished_at=clock_timestamp()
            WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3", &[&token.run_id,&token.task_key,&token.attempt_id,&request.code,&digest]).map_err(db_error)?;
        let task_spec: TaskSpec = decoded(task.get("spec"))?;
        retry_task(
            &mut transaction,
            token.run_id,
            &token.task_key,
            &task_spec,
            token.execution_epoch,
            request.retryable,
            &request.code,
        )?;
        commit(transaction)
    }

    pub fn control(&self, run_id: Uuid, request: &ControlRequest) -> Result<RunView> {
        if request.operation_id.is_nil()
            || request.expected_control_epoch < 0
            || !["pause", "resume", "cancel"].contains(&request.action.as_str())
        {
            return Err(error("CONTROL_INVALID", "Invalid control operation"));
        }
        let digest = request_digest(request)?;
        let mut client = self.connect()?;
        let mut transaction = client.transaction().map_err(db_error)?;
        let run = run_lock(&mut transaction, run_id)?;
        if let Some(row)=transaction.query_opt("SELECT request_digest,result FROM control_receipts WHERE run_id=$1 AND operation_id=$2",
                                               &[&run_id,&request.operation_id]).map_err(db_error)? {
            if row.get::<_,String>(0)!=digest { return Err(error("IDEMPOTENCY_CONFLICT","Control identity was reused with other content")); }
            let result=decoded(row.get(1))?; commit(transaction)?; return Ok(result);
        }
        if run.get::<_, i64>("control_epoch") != request.expected_control_epoch {
            return Err(error("CONTROL_CONFLICT", "Control epoch no longer matches"));
        }
        let state: String = run.get("state");
        if !(["pause", "cancel"].contains(&request.action.as_str()) && state == "running"
            || request.action == "cancel" && state == "paused"
            || request.action == "resume" && state == "paused")
        {
            return Err(error(
                "CONTROL_INVALID",
                "Control action is not legal in the current run state",
            ));
        }
        let next = match request.action.as_str() {
            "pause" => "paused",
            "resume" => "running",
            _ => "cancelled",
        };
        transaction
            .execute(
                "UPDATE runs SET state=$2,control_epoch=control_epoch+1 WHERE run_id=$1",
                &[&run_id, &next],
            )
            .map_err(db_error)?;
        if request.action != "resume" {
            let code = if request.action == "pause" {
                "PAUSED"
            } else {
                "CANCELLED"
            };
            transaction.execute("UPDATE attempts SET state='abandoned',error=$2,finished_at=clock_timestamp() WHERE run_id=$1 AND state='running'",
                                &[&run_id,&code]).map_err(db_error)?;
            if request.action == "pause" {
                transaction.execute("UPDATE tasks SET state='retry_wait',error='PAUSED',available_at=clock_timestamp() WHERE run_id=$1 AND state='running'",&[&run_id]).map_err(db_error)?;
            } else {
                transaction.execute("UPDATE tasks SET state='cancelled',error='CANCELLED' WHERE run_id=$1 AND state IN ('pending','retry_wait','running')",&[&run_id]).map_err(db_error)?;
            }
        }
        event(
            &mut transaction,
            run_id,
            &format!("run.{}", request.action),
            json!({"control_epoch":request.expected_control_epoch+1}),
        )?;
        if request.action == "resume" {
            if let Some(exhausted)=transaction.query_opt("SELECT task_key FROM tasks WHERE run_id=$1 AND state IN ('pending','retry_wait')
                AND execution_epoch>=(spec->>'max_attempts')::int ORDER BY task_key LIMIT 1 FOR UPDATE",&[&run_id]).map_err(db_error)? {
                let key:String=exhausted.get(0);
                fail_run(&mut transaction,run_id,&key,"ATTEMPT_LIMIT")?;
            }
        }
        let result = view(&mut transaction, run_id)?;
        transaction.execute("INSERT INTO control_receipts(run_id,operation_id,request_digest,result) VALUES($1,$2,$3,$4)",
                            &[&run_id,&request.operation_id,&digest,&encoded(&result)?]).map_err(db_error)?;
        commit(transaction)?;
        Ok(result)
    }

    pub fn recover(&self) -> Result<usize> {
        let mut client = self.connect()?;
        let mut recovered = 0;
        for _ in 0..100 {
            let mut transaction = client.transaction().map_err(db_error)?;
            let Some(run) = transaction
                .query_opt(
                    "SELECT r.run_id FROM runs r WHERE r.state='running'
                AND EXISTS(SELECT 1 FROM attempts a WHERE a.run_id=r.run_id AND a.state='running'
                  AND (a.lease_until<=clock_timestamp() OR a.deadline<=clock_timestamp()))
                ORDER BY r.run_id FOR UPDATE OF r SKIP LOCKED LIMIT 1",
                    &[],
                )
                .map_err(db_error)?
            else {
                commit(transaction)?;
                break;
            };
            let run_id: Uuid = run.get(0);
            let active=transaction.query("SELECT * FROM tasks WHERE run_id=$1 AND state='running' ORDER BY task_key FOR UPDATE",&[&run_id]).map_err(db_error)?;
            for task in active {
                let key: String = task.get("task_key");
                let attempt_id: Uuid = task.get("current_attempt_id");
                transaction.query_one("SELECT attempt_id FROM attempts WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3 FOR UPDATE",
                                      &[&run_id,&key,&attempt_id]).map_err(db_error)?;
                let expired:bool=transaction.query_one("SELECT lease_until<=clock_timestamp() OR deadline<=clock_timestamp() FROM attempts
                    WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3",&[&run_id,&key,&attempt_id]).map_err(db_error)?.get(0);
                if !expired {
                    continue;
                }
                transaction.execute("UPDATE attempts SET state='abandoned',error='LEASE_EXPIRED',finished_at=clock_timestamp()
                    WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3",&[&run_id,&key,&attempt_id]).map_err(db_error)?;
                let spec: TaskSpec = decoded(task.get("spec"))?;
                let epoch: i64 = task.get("execution_epoch");
                event(
                    &mut transaction,
                    run_id,
                    "attempt.abandoned",
                    json!({"task_key":key,"attempt_id":attempt_id,"code":"LEASE_EXPIRED"}),
                )?;
                retry_task(
                    &mut transaction,
                    run_id,
                    &key,
                    &spec,
                    epoch,
                    true,
                    "LEASE_EXPIRED",
                )?;
                recovered += 1;
                let running: bool = transaction
                    .query_one(
                        "SELECT state='running' FROM runs WHERE run_id=$1",
                        &[&run_id],
                    )
                    .map_err(db_error)?
                    .get(0);
                if !running {
                    break;
                }
            }
            commit(transaction)?;
        }
        Ok(recovered)
    }

    pub fn events(&self, run_id: Uuid, after: i64, limit: i64) -> Result<Vec<RuntimeEvent>> {
        if after < 0 || !(1..=1000).contains(&limit) {
            return Err(error(
                "CURSOR_INVALID",
                "Expected after>=0 and limit 1..1000",
            ));
        }
        let mut client = self.connect()?;
        if client
            .query_opt("SELECT run_id FROM runs WHERE run_id=$1", &[&run_id])
            .map_err(db_error)?
            .is_none()
        {
            return Err(error("RUN_NOT_FOUND", "Unknown run"));
        }
        client.query("SELECT seq,event_id,kind,payload FROM events WHERE run_id=$1 AND seq>$2 ORDER BY seq LIMIT $3",
                     &[&run_id,&after,&limit]).map_err(db_error)?.into_iter().map(|row|Ok(RuntimeEvent {
            seq:row.get(0),event_id:row.get(1),kind:row.get(2),payload:row.get(3),
        })).collect()
    }
}
