//! Real PostgreSQL protocol checks. Native geometry is verified by the service,
//! so output fixtures here exercise only immutable metadata and lease authority.

use core_domain::{GeneralAcceptance, NodeAcceptance};
use core_runtime::*;
use core_storage::HeadToken;
use postgres::{Client, NoTls};
use std::{collections::BTreeSet, sync::Arc, thread};
use uuid::Uuid;

fn repository() -> Option<Repository> {
    let Ok(url) = std::env::var("DEEP3D_TEST_DATABASE_URL") else {
        eprintln!("NOT RUN: real PostgreSQL tests require DEEP3D_TEST_DATABASE_URL");
        return None;
    };
    let repository = Repository::new(&url, 30).unwrap();
    repository.migrate().unwrap();
    Some(repository)
}

fn hash(ch: char) -> String {
    format!("sha256:{}", ch.to_string().repeat(64))
}

fn task(key: &str) -> TaskSpec {
    TaskSpec {
        key: key.into(),
        depends_on: vec![],
        source_task: None,
        changes_mm: [("thickness_mm".into(), 16.0)].into(),
        acceptance: None,
        max_attempts: 3,
        timeout_seconds: 600,
    }
}

fn spec(tasks: Vec<TaskSpec>) -> RunSpec {
    RunSpec {
        schema_version: "deep3d.durable-run/1".into(),
        run_id: Uuid::new_v4(),
        project_id: Uuid::new_v4(),
        submission_sha256: hash('a'),
        source: ArtifactSource {
            path: "objects/input.deep3d".into(),
            sha256: hash('b'),
            head: HeadToken {
                revision_id: Uuid::new_v4(),
                generation: 0,
            },
        },
        tasks,
        authority: None,
    }
}

struct Cleanup(Repository, Uuid);
impl Drop for Cleanup {
    fn drop(&mut self) {
        if let Ok(run) = self.0.get_run(self.1) {
            if ["running", "paused"].contains(&run.state.as_str()) {
                let _ = self.0.control(
                    self.1,
                    &ControlRequest {
                        operation_id: Uuid::new_v4(),
                        expected_control_epoch: run.control_epoch,
                        action: "cancel".into(),
                    },
                );
            }
        }
    }
}

fn submitted(repo: &Repository, spec: &RunSpec) -> Cleanup {
    repo.submit(spec).unwrap();
    Cleanup(repo.clone(), spec.run_id)
}

fn request(run_id: Uuid, worker: &str) -> ClaimRequest {
    ClaimRequest {
        claim_id: Uuid::new_v4(),
        worker_id: worker.into(),
        run_id: Some(run_id),
    }
}

fn claim(repo: &Repository, run_id: Uuid, worker: &str) -> Lease {
    repo.claim(&request(run_id, worker)).unwrap().unwrap()
}

fn output(spec: &RunSpec, lease: &Lease) -> TaskOutput {
    TaskOutput {
        schema_version: "deep3d.task-output/1".into(),
        project_id: spec.project_id,
        source: ArtifactSource {
            path: format!("objects/{}.deep3d", lease.token.attempt_id),
            sha256: hash('c'),
            head: HeadToken {
                revision_id: Uuid::new_v4(),
                generation: lease.source.head.generation + 1,
            },
        },
        materialization: hash('d'),
        step_sha256: hash('e'),
    }
}

fn expire(lease: &Lease) {
    let mut client =
        Client::connect(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), NoTls).unwrap();
    client
        .execute(
            "UPDATE deep3d_runtime.attempts SET lease_until=clock_timestamp()-interval '1 second'
        WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3",
            &[
                &lease.token.run_id,
                &lease.token.task_key,
                &lease.token.attempt_id,
            ],
        )
        .unwrap();
}

fn make_retry_ready(lease: &Lease) {
    let mut client =
        Client::connect(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), NoTls).unwrap();
    client.execute("UPDATE deep3d_runtime.tasks SET available_at=clock_timestamp() WHERE run_id=$1 AND task_key=$2",
                   &[&lease.token.run_id,&lease.token.task_key]).unwrap();
}

#[test]
fn generic_parameter_tasks_encode_independent_optional_acceptance() {
    let node = Uuid::new_v4();
    let acceptance = GeneralAcceptance::new(
        [(
            node,
            NodeAcceptance {
                solid_count: 1,
                volume_mm3: Some(1200.0),
                bounds_mm: None,
            },
        )]
        .into(),
    );
    let mut generic = task("block");
    generic.changes_mm = [("block width mm".into(), 24.0)].into();
    generic.acceptance = Some(acceptance.clone());
    validate_tasks(&[generic.clone()]).unwrap();
    let json = serde_json::to_value(&generic).unwrap();
    assert_eq!(
        json["acceptance"],
        serde_json::to_value(&acceptance).unwrap()
    );
    assert_eq!(serde_json::from_value::<TaskSpec>(json).unwrap(), generic);
    assert!(serde_json::to_value(task("old-flange"))
        .unwrap()
        .get("acceptance")
        .is_none());
    generic.changes_mm = [("bad\nparameter".into(), 24.0)].into();
    assert!(validate_tasks(&[generic.clone()]).is_err());
    generic.changes_mm = [("width_mm".into(), 24.0)].into();
    generic.acceptance.as_mut().unwrap().nodes.clear();
    assert!(validate_tasks(&[generic]).is_err());
}

#[test]
fn generic_goal_is_immutable_and_survives_claim_and_heartbeat() {
    let Some(repo) = repository() else {
        return;
    };
    let node = Uuid::new_v4();
    let mut definition = task("generic-block");
    definition.changes_mm = [("block_width_mm".into(), 24.0)].into();
    definition.acceptance = Some(GeneralAcceptance::new(
        [(
            node,
            NodeAcceptance {
                solid_count: 1,
                volume_mm3: Some(1200.0),
                bounds_mm: Some([0.0, 0.0, 0.0, 24.0, 10.0, 5.0]),
            },
        )]
        .into(),
    ));
    let frozen = spec(vec![definition.clone()]);
    let _cleanup = submitted(&repo, &frozen);
    let leased = claim(&repo, frozen.run_id, "generic-owner");
    assert_eq!(leased.acceptance, definition.acceptance);
    assert_eq!(leased.changes_mm, definition.changes_mm);
    assert_eq!(
        repo.heartbeat(&leased.token).unwrap().acceptance,
        definition.acceptance
    );
    let mut changed = frozen.clone();
    changed.tasks[0]
        .acceptance
        .as_mut()
        .unwrap()
        .nodes
        .get_mut(&node)
        .unwrap()
        .volume_mm3 = Some(1.0);
    assert_eq!(
        repo.submit(&changed).unwrap_err().code,
        "IDEMPOTENCY_CONFLICT"
    );
    let original = repo.get_run(frozen.run_id).unwrap();
    assert_eq!(original.spec.tasks[0].acceptance, definition.acceptance);
}

#[test]
fn immutable_submission_and_invalid_graphs_are_atomic() {
    let Some(repo) = repository() else {
        return;
    };
    let mut spec = spec(vec![task("first"), task("second")]);
    let _cleanup = submitted(&repo, &spec);
    assert_eq!(repo.submit(&spec).unwrap().event_seq, 1);
    spec.tasks[0].changes_mm.insert("thickness_mm".into(), 18.0);
    assert_eq!(repo.submit(&spec).unwrap_err().code, "IDEMPOTENCY_CONFLICT");
    let mut invalid = crate::spec(vec![task("a"), task("b")]);
    invalid.tasks[0].depends_on.push("b".into());
    invalid.tasks[1].depends_on.push("a".into());
    assert_eq!(repo.submit(&invalid).unwrap_err().code, "PLAN_INVALID");
    assert_eq!(
        repo.get_run(invalid.run_id).unwrap_err().code,
        "RUN_NOT_FOUND"
    );
}

#[test]
fn concurrent_claims_and_same_claim_identity_never_double_lease() {
    let Some(repo) = repository() else {
        return;
    };
    let spec = spec(
        (0..20)
            .map(|index| task(&format!("task-{index:02}")))
            .collect(),
    );
    let _cleanup = submitted(&repo, &spec);
    let repo = Arc::new(repo);
    let joins: Vec<_> = (0..24)
        .map(|index| {
            let repo = Arc::clone(&repo);
            let run_id = spec.run_id;
            thread::spawn(move || {
                repo.claim(&request(run_id, &format!("worker-{index}")))
                    .unwrap()
            })
        })
        .collect();
    let mut leases: Vec<_> = joins
        .into_iter()
        .filter_map(|join| join.join().unwrap())
        .collect();
    while leases.len() < 8 {
        if let Some(lease) = repo
            .claim(&request(spec.run_id, &format!("fill-{}", leases.len())))
            .unwrap()
        {
            leases.push(lease);
        }
    }
    assert_eq!(leases.len(), 8);
    assert_eq!(
        leases
            .iter()
            .map(|lease| &lease.token.task_key)
            .collect::<BTreeSet<_>>()
            .len(),
        8
    );
    assert!(repo
        .claim(&request(spec.run_id, "ninth"))
        .unwrap()
        .is_none());
    let view = repo.get_run(spec.run_id).unwrap();
    assert_eq!(view.attempts.len(), 8);
    assert_eq!(
        view.tasks
            .iter()
            .filter(|task| task.state == "running")
            .count(),
        8
    );
    // Same stable key concurrent retry returns one exact attempt.
    let another = crate::spec(vec![task("single")]);
    let _another_cleanup = submitted(&repo, &another);
    let shared = request(another.run_id, "same-worker");
    let joins: Vec<_> = (0..8)
        .map(|_| {
            let repo = Arc::clone(&repo);
            let shared = shared.clone();
            thread::spawn(move || repo.claim(&shared).unwrap().unwrap().token)
        })
        .collect();
    let tokens: Vec<_> = joins.into_iter().map(|join| join.join().unwrap()).collect();
    assert!(tokens.iter().all(|token| token == &tokens[0]));
    assert_eq!(repo.get_run(another.run_id).unwrap().attempts.len(), 1);
}

#[test]
fn dependencies_bind_completed_source_and_completed_receipt_survives_expiry() {
    let Some(repo) = repository() else {
        return;
    };
    let mut child = task("second");
    child.depends_on.push("first".into());
    child.source_task = Some("first".into());
    let spec = spec(vec![task("first"), child]);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "one");
    assert_eq!(first.token.task_key, "first");
    assert!(repo.claim(&request(spec.run_id, "two")).unwrap().is_none());
    let first_output = output(&spec, &first);
    let first_digest = hash('1');
    assert!(repo
        .completion(&first.token, &first_digest)
        .unwrap()
        .is_none());
    repo.complete(&first.token, &first_output, &first_digest)
        .unwrap();
    assert_eq!(
        repo.complete(&first.token, &first_output, &first_digest)
            .unwrap(),
        first_output
    );
    let second = claim(&repo, spec.run_id, "two");
    assert_eq!(second.source, first_output.source);
    let second_output = output(&spec, &second);
    repo.complete(&second.token, &second_output, &hash('2'))
        .unwrap();
    let view = repo.get_run(spec.run_id).unwrap();
    assert_eq!(view.state, "completed");
    expire(&first);
    assert_eq!(
        repo.completion(&first.token, &first_digest).unwrap(),
        Some(first_output.clone())
    );
    assert_eq!(
        repo.complete(&first.token, &first_output, &first_digest)
            .unwrap(),
        first_output
    );
    assert_eq!(
        repo.completion(&first.token, &hash('3')).unwrap_err().code,
        "IDEMPOTENCY_CONFLICT"
    );
    assert_eq!(
        repo.heartbeat(&first.token).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    let events = repo.events(spec.run_id, 0, 100).unwrap();
    assert_eq!(events.len() as i64, view.event_seq);
    assert_eq!(events.last().unwrap().kind, "run.completed");
    assert_eq!(
        events.iter().map(|event| event.seq).collect::<Vec<_>>(),
        (1..=view.event_seq).collect::<Vec<_>>()
    );
    assert_eq!(
        repo.events(spec.run_id, events[1].seq, 100).unwrap().len(),
        events.len() - 2
    );
}

#[test]
fn expired_execution_cannot_renew_or_finish_and_recovery_fences_old_worker() {
    let Some(repo) = repository() else {
        return;
    };
    let spec = spec(vec![task("part")]);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "old-worker");
    expire(&first);
    assert_eq!(
        repo.heartbeat(&first.token).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    assert_eq!(
        repo.complete(&first.token, &output(&spec, &first), &hash('1'))
            .unwrap_err()
            .code,
        "STALE_EXECUTION_LEASE"
    );
    // Another concurrent recovery test may have already reclaimed this attempt.
    repo.recover().unwrap();
    make_retry_ready(&first);
    let replacement = claim(&repo, spec.run_id, "replacement");
    assert_eq!(replacement.token.execution_epoch, 2);
    assert_ne!(replacement.token.attempt_id, first.token.attempt_id);
    assert_eq!(
        repo.source_for(&first.token).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    repo.complete(&replacement.token, &output(&spec, &replacement), &hash('2'))
        .unwrap();
    assert_eq!(repo.get_run(spec.run_id).unwrap().state, "completed");
}

#[test]
fn pause_resume_cancel_revoke_authority_and_control_retries_are_idempotent() {
    let Some(repo) = repository() else {
        return;
    };
    let spec = spec(vec![task("part")]);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "worker");
    let pause = ControlRequest {
        operation_id: Uuid::new_v4(),
        expected_control_epoch: 0,
        action: "pause".into(),
    };
    let paused = repo.control(spec.run_id, &pause).unwrap();
    assert_eq!(paused.state, "paused");
    assert_eq!(paused.control_epoch, 1);
    assert_eq!(
        repo.control(spec.run_id, &pause).unwrap().event_seq,
        paused.event_seq
    );
    assert!(repo
        .claim(&request(spec.run_id, "other"))
        .unwrap()
        .is_none());
    assert_eq!(
        repo.heartbeat(&first.token).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    let resumed = repo
        .control(
            spec.run_id,
            &ControlRequest {
                operation_id: Uuid::new_v4(),
                expected_control_epoch: 1,
                action: "resume".into(),
            },
        )
        .unwrap();
    assert_eq!(resumed.state, "running");
    let second = claim(&repo, spec.run_id, "other");
    assert_eq!(second.token.execution_epoch, 2);
    assert_eq!(second.token.control_epoch, 2);
    assert_eq!(
        repo.complete(&first.token, &output(&spec, &first), &hash('1'))
            .unwrap_err()
            .code,
        "STALE_EXECUTION_LEASE"
    );
    let cancelled = repo
        .control(
            spec.run_id,
            &ControlRequest {
                operation_id: Uuid::new_v4(),
                expected_control_epoch: 2,
                action: "cancel".into(),
            },
        )
        .unwrap();
    assert_eq!(cancelled.state, "cancelled");
    assert_eq!(
        repo.complete(&second.token, &output(&spec, &second), &hash('2'))
            .unwrap_err()
            .code,
        "STALE_EXECUTION_LEASE"
    );
    assert_eq!(
        repo.control(
            spec.run_id,
            &ControlRequest {
                operation_id: Uuid::new_v4(),
                expected_control_epoch: 0,
                action: "resume".into()
            }
        )
        .unwrap_err()
        .code,
        "CONTROL_CONFLICT"
    );
}

#[test]
fn retry_budget_and_nonretryable_failure_terminate_dependents() {
    let Some(repo) = repository() else {
        return;
    };
    let mut first = task("first");
    first.max_attempts = 1;
    let mut child = task("child");
    child.depends_on.push("first".into());
    let spec = spec(vec![first, child]);
    let _cleanup = submitted(&repo, &spec);
    let lease = claim(&repo, spec.run_id, "worker");
    expire(&lease);
    repo.recover().unwrap();
    let view = repo.get_run(spec.run_id).unwrap();
    assert_eq!(view.state, "failed");
    assert!(view
        .tasks
        .iter()
        .any(|task| task.spec.key == "first" && task.state == "failed"));
    assert!(view
        .tasks
        .iter()
        .any(|task| task.spec.key == "child" && task.state == "cancelled"));
    let another = crate::spec(vec![task("one"), task("two")]);
    let _cleanup2 = submitted(&repo, &another);
    let lease = claim(&repo, another.run_id, "worker");
    let failure = FailureRequest {
        lease: lease.token,
        code: "GEOMETRY_REJECTED".into(),
        retryable: false,
    };
    repo.fail(&failure).unwrap();
    repo.fail(&failure).unwrap();
    assert_eq!(repo.get_run(another.run_id).unwrap().state, "failed");
}

#[test]
fn empty_claim_receipts_do_not_later_take_a_task_and_foreign_tokens_fail() {
    let Some(repo) = repository() else {
        return;
    };
    let spec = spec(vec![task("part")]);
    let _cleanup = submitted(&repo, &spec);
    repo.control(
        spec.run_id,
        &ControlRequest {
            operation_id: Uuid::new_v4(),
            expected_control_epoch: 0,
            action: "pause".into(),
        },
    )
    .unwrap();
    let no_work = request(spec.run_id, "worker");
    assert!(repo.claim(&no_work).unwrap().is_none());
    repo.control(
        spec.run_id,
        &ControlRequest {
            operation_id: Uuid::new_v4(),
            expected_control_epoch: 1,
            action: "resume".into(),
        },
    )
    .unwrap();
    assert!(repo.claim(&no_work).unwrap().is_none());
    let real = claim(&repo, spec.run_id, "worker");
    let mut foreign = real.token.clone();
    foreign.run_id = Uuid::new_v4();
    assert_eq!(repo.heartbeat(&foreign).unwrap_err().code, "RUN_NOT_FOUND");
    foreign = real.token.clone();
    foreign.worker_id = "impostor".into();
    assert_eq!(
        repo.heartbeat(&foreign).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    let changed = ClaimRequest {
        run_id: None,
        ..no_work
    };
    assert_eq!(
        repo.claim(&changed).unwrap_err().code,
        "IDEMPOTENCY_CONFLICT"
    );
    assert_eq!(
        repo.events(spec.run_id, -1, 100).unwrap_err().code,
        "CURSOR_INVALID"
    );
}

#[test]
fn retryable_failure_receipts_cannot_mutate_the_replacement_attempt() {
    let Some(repo) = repository() else {
        return;
    };
    let spec = spec(vec![task("part")]);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "original");
    let failure = FailureRequest {
        lease: first.token.clone(),
        code: "WORKER_CRASH".into(),
        retryable: true,
    };
    repo.fail(&failure).unwrap();
    repo.fail(&failure).unwrap();
    let mut changed = failure.clone();
    changed.retryable = false;
    assert_eq!(
        repo.fail(&changed).unwrap_err().code,
        "IDEMPOTENCY_CONFLICT"
    );
    make_retry_ready(&first);
    let second = claim(&repo, spec.run_id, "replacement");
    assert_eq!(second.token.execution_epoch, 2);
    repo.fail(&failure).unwrap();
    assert_eq!(repo.get_run(spec.run_id).unwrap().tasks[0].state, "running");
    repo.complete(&second.token, &output(&spec, &second), &hash('2'))
        .unwrap();
    assert_eq!(repo.get_run(spec.run_id).unwrap().state, "completed");
}

#[test]
fn pausing_the_last_attempt_has_an_explicit_exhausted_resume_result() {
    let Some(repo) = repository() else {
        return;
    };
    let mut part = task("part");
    part.max_attempts = 1;
    let spec = spec(vec![part]);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "worker");
    repo.control(
        spec.run_id,
        &ControlRequest {
            operation_id: Uuid::new_v4(),
            expected_control_epoch: 0,
            action: "pause".into(),
        },
    )
    .unwrap();
    let resume = ControlRequest {
        operation_id: Uuid::new_v4(),
        expected_control_epoch: 1,
        action: "resume".into(),
    };
    let view = repo.control(spec.run_id, &resume).unwrap();
    assert_eq!(view.state, "failed");
    assert_eq!(view.tasks[0].error.as_deref(), Some("ATTEMPT_LIMIT"));
    assert_eq!(
        repo.control(spec.run_id, &resume).unwrap().event_seq,
        view.event_seq
    );
    assert_eq!(
        repo.heartbeat(&first.token).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
}

#[test]
fn heartbeat_checks_database_time_after_waiting_for_the_run_lock() {
    let Some(repo) = repository() else {
        return;
    };
    let spec = spec(vec![task("part")]);
    let _cleanup = submitted(&repo, &spec);
    let lease = claim(&repo, spec.run_id, "worker");
    let mut connection =
        Client::connect(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), NoTls).unwrap();
    let mut held = connection.transaction().unwrap();
    held.query_one(
        "SELECT run_id FROM deep3d_runtime.runs WHERE run_id=$1 FOR UPDATE",
        &[&spec.run_id],
    )
    .unwrap();
    held.execute("UPDATE deep3d_runtime.attempts SET lease_until=clock_timestamp()+interval '400 milliseconds'
                  WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3",
                 &[&lease.token.run_id, &lease.token.task_key, &lease.token.attempt_id]).unwrap();
    let worker_repo = repo.clone();
    let token = lease.token.clone();
    let waiting = thread::spawn(move || worker_repo.heartbeat(&token));
    thread::sleep(std::time::Duration::from_millis(650));
    held.commit().unwrap();
    assert_eq!(
        waiting.join().unwrap().unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    assert_eq!(
        repo.source_for(&lease.token).unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
}

#[test]
fn one_exhausted_expired_task_cancels_other_expired_tasks_without_requeue() {
    let Some(repo) = repository() else {
        return;
    };
    let mut a = task("a");
    a.max_attempts = 1;
    let mut b = task("b");
    b.max_attempts = 1;
    let spec = spec(vec![a, b]);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "one");
    let second = claim(&repo, spec.run_id, "two");
    expire(&first);
    expire(&second);
    for _ in 0..20 {
        repo.recover().unwrap();
        if repo.get_run(spec.run_id).unwrap().state == "failed" {
            break;
        }
        thread::sleep(std::time::Duration::from_millis(20));
    }
    let view = repo.get_run(spec.run_id).unwrap();
    assert_eq!(view.state, "failed");
    assert_eq!(
        view.tasks
            .iter()
            .filter(|task| task.state == "failed")
            .count(),
        1
    );
    assert_eq!(
        view.tasks
            .iter()
            .filter(|task| task.state == "cancelled")
            .count(),
        1
    );
    assert!(view
        .attempts
        .iter()
        .all(|attempt| attempt["state"] == "abandoned"));
    assert!(repo
        .claim(&request(spec.run_id, "third"))
        .unwrap()
        .is_none());
}
