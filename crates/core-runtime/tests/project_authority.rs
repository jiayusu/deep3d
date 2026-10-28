//! Real PostgreSQL checks of goal/publication authority, not CAD qualification.
//!
//! Geometry and evidence fixtures below are deliberately synthetic protocol
//! metadata. The HTTP coordinator must inspect real native packages and reports
//! before it constructs PublicationEvidence; these tests do not replace that.

use core_domain::{Acceptance, GeneralAcceptance, NodeAcceptance, PrecisionProfile};
use core_runtime::*;
use core_storage::HeadToken;
use postgres::{Client, NoTls};
use serde::Serialize;
use serde_json::{json, Value};
use std::{sync::Barrier, thread, time::Duration};
use uuid::Uuid;

fn repository() -> Option<Repository> {
    let Ok(url) = std::env::var("DEEP3D_TEST_DATABASE_URL") else {
        eprintln!("NOT RUN: project authority requires DEEP3D_TEST_DATABASE_URL");
        return None;
    };
    let repo = Repository::new(&url, 30).unwrap();
    repo.migrate().unwrap();
    Some(repo)
}

fn value(item: &impl Serialize) -> Value {
    serde_json::to_value(item).unwrap()
}

fn hash(label: &str) -> String {
    core_domain::digest_bytes(label.as_bytes())
}

fn source(label: &str, generation: u64) -> ArtifactSource {
    let sha256 = hash(label);
    ArtifactSource {
        path: format!("objects/{}.deep3d", &sha256[7..]),
        sha256,
        head: HeadToken {
            revision_id: Uuid::new_v4(),
            generation,
        },
    }
}

fn definition() -> GoalDefinition {
    let node = Uuid::new_v4();
    serde_json::from_value(json!({
        "schema_version":"deep3d.goal/1",
        "original_request":"Protocol fixture: one independently specified 24 mm block",
        "source_refs":["operator:protocol-fixture"],
        "acceptance":GeneralAcceptance::new([(
            node,
            NodeAcceptance {
                solid_count:1,
                volume_mm3:Some(1200.0),
                bounds_mm:Some([0.0,0.0,0.0,24.0,10.0,5.0]),
            },
        )].into()),
        "precision":PrecisionProfile::default(),
        "unresolved":[],
    }))
    .unwrap()
}

fn update_request(expected_goal: Option<Uuid>, generation: u64) -> UpdateGoalRequest {
    UpdateGoalRequest {
        operation_id: Uuid::new_v4(),
        expected_goal,
        expected_generation: generation,
        definition: definition(),
    }
}

fn goal_id(goal: &GoalRevision) -> Uuid {
    serde_json::from_value(value(goal)["revision_id"].clone()).unwrap()
}

fn public_id(project: &ProjectView) -> Option<Uuid> {
    serde_json::from_value(value(project)["public_head"].clone()).unwrap()
}

fn public_generation(project: &ProjectView) -> u64 {
    value(project)["public_generation"]
        .as_str()
        .expect("Public generation must not lose precision in JSON")
        .parse()
        .unwrap()
}

struct Fixture {
    id: Uuid,
    initial: ArtifactSource,
    goal: GoalRevision,
}

fn fixture(repo: &Repository) -> Fixture {
    let id = Uuid::new_v4();
    let initial = source(&format!("protocol-initial:{id}"), 0);
    let project = repo.register_project(id, &initial, Uuid::new_v4()).unwrap();
    assert!(public_id(&project).is_none());
    assert_eq!(public_generation(&project), 0);
    let goal = repo.update_goal(id, &update_request(None, 0)).unwrap();
    Fixture { id, initial, goal }
}

fn task(goal: &GoalRevision, key: &str) -> TaskSpec {
    let Acceptance::Mechanical(acceptance) = &goal.definition.acceptance else {
        panic!("Protocol fixture uses mechanical acceptance")
    };
    TaskSpec {
        key: key.into(),
        depends_on: vec![],
        source_task: None,
        changes_mm: [("width_mm".into(), 24.0)].into(),
        acceptance: Some(acceptance.clone()),
        max_attempts: 3,
        timeout_seconds: 600,
    }
}

fn run_spec(
    fixture: &Fixture,
    source: &ArtifactSource,
    expected_commit: Option<Uuid>,
    expected_generation: u64,
) -> RunSpec {
    RunSpec {
        schema_version: "deep3d.durable-run/1".into(),
        run_id: Uuid::new_v4(),
        project_id: fixture.id,
        submission_sha256: hash(&format!("protocol-submission:{}", Uuid::new_v4())),
        source: source.clone(),
        tasks: vec![task(&fixture.goal, "publish")],
        authority: Some(RunBinding {
            goal_revision_id: goal_id(&fixture.goal),
            expected_public_commit: expected_commit,
            expected_public_generation: expected_generation,
            publish_task: "publish".into(),
        }),
    }
}

struct Cleanup(Repository, Uuid);
impl Drop for Cleanup {
    fn drop(&mut self) {
        if let Ok(run) = self.0.get_run(self.1) {
            if matches!(run.state.as_str(), "running" | "paused") {
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

fn claim(repo: &Repository, run: Uuid, worker: &str) -> Lease {
    repo.claim(&ClaimRequest {
        claim_id: Uuid::new_v4(),
        worker_id: worker.into(),
        run_id: Some(run),
    })
    .unwrap()
    .unwrap()
}

fn output(fixture: &Fixture, lease: &Lease) -> TaskOutput {
    TaskOutput {
        schema_version: "deep3d.task-output/1".into(),
        project_id: fixture.id,
        source: source(
            &format!("protocol-output:{}", lease.token.attempt_id),
            lease.source.head.generation + 1,
        ),
        materialization: hash(&format!(
            "protocol-materialization:{}",
            lease.token.attempt_id
        )),
        step_sha256: hash(&format!("protocol-step:{}", lease.token.attempt_id)),
    }
}

fn evidence(goal: &GoalRevision, output: &TaskOutput) -> PublicationEvidence {
    PublicationEvidence {
        schema_version: "deep3d.publication-evidence/1".into(),
        goal_revision_id: goal_id(goal),
        acceptance_sha256: request_digest(&goal.definition.acceptance).unwrap(),
        precision_sha256: request_digest(&goal.definition.precision).unwrap(),
        source_sha256: output.source.sha256.clone(),
        materialization: output.materialization.clone(),
        native_report_sha256: hash(&format!("protocol-native-report:{}", output.source.sha256)),
        step_report_sha256: hash(&format!("protocol-step-report:{}", output.source.sha256)),
        native_geometry_sha256: hash(&format!("protocol-native:{}", output.source.sha256)),
        step_geometry_sha256: output.step_sha256.clone(),
        assembly_sha256: Some(hash(&format!("protocol-assembly:{}", output.source.sha256))),
    }
}

fn publish(
    repo: &Repository,
    fixture: &Fixture,
    lease: &Lease,
) -> (TaskOutput, PublicationEvidence, String) {
    let output = output(fixture, lease);
    let evidence = evidence(&fixture.goal, &output);
    let digest = hash(&format!("protocol-completion:{}", lease.token.attempt_id));
    let accepted = repo
        .complete_with_publication(&lease.token, &output, &digest, Some(&evidence))
        .unwrap();
    assert_eq!(accepted, output);
    (output, evidence, digest)
}

fn expire(lease: &Lease) {
    let mut db =
        Client::connect(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), NoTls).unwrap();
    db.execute(
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

#[test]
fn registration_and_goal_revisions_have_stable_receipts_and_immutable_history() {
    let Some(repo) = repository() else { return };
    let id = Uuid::new_v4();
    let original = source(&format!("registration:{id}"), 0);
    let operation = Uuid::new_v4();
    let registered = repo.register_project(id, &original, operation).unwrap();
    assert_eq!(
        value(&repo.register_project(id, &original, operation).unwrap()),
        value(&registered)
    );
    let changed = source(&format!("changed-registration:{id}"), 0);
    assert!(repo.register_project(id, &changed, operation).is_err());
    assert!(repo.register_project(id, &changed, Uuid::new_v4()).is_err());
    let request = update_request(None, 0);
    let first = repo.update_goal(id, &request).unwrap();
    assert_eq!(
        value(&repo.update_goal(id, &request).unwrap()),
        value(&first)
    );
    let mut reused = request.clone();
    reused.definition.original_request.push_str(" changed");
    assert_eq!(
        repo.update_goal(id, &reused).unwrap_err().code,
        "IDEMPOTENCY_CONFLICT"
    );
    let second_request = update_request(Some(goal_id(&first)), 1);
    let second = repo.update_goal(id, &second_request).unwrap();
    assert_ne!(goal_id(&first), goal_id(&second));
    assert_eq!(
        value(&repo.get_goal(id, goal_id(&first)).unwrap()),
        value(&first)
    );
    assert_eq!(
        value(&repo.update_goal(id, &request).unwrap()),
        value(&first),
        "A historical successful receipt is not a new GoalHead write"
    );
    assert_eq!(
        value(&repo.get_project(id).unwrap())["goal_head"],
        goal_id(&second).to_string()
    );
    assert_eq!(
        repo.update_goal(id, &update_request(Some(goal_id(&first)), 1))
            .unwrap_err()
            .code,
        "GOAL_CONFLICT"
    );
}

#[test]
fn bound_plan_rejects_changed_source_bytes_head_path_goal_and_public_base() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let original = run_spec(&fixture, &fixture.initial, None, 0);
    for variant in [
        "bytes",
        "revision",
        "generation",
        "path",
        "goal",
        "public",
        "public_generation",
    ] {
        let mut changed = original.clone();
        changed.run_id = Uuid::new_v4();
        match variant {
            "bytes" => changed.source.sha256 = hash("another source"),
            "revision" => changed.source.head.revision_id = Uuid::new_v4(),
            "generation" => changed.source.head.generation += 1,
            "path" => changed.source.path = "objects/alias.deep3d".into(),
            "goal" => changed.authority.as_mut().unwrap().goal_revision_id = Uuid::new_v4(),
            "public" => {
                changed.authority.as_mut().unwrap().expected_public_commit = Some(Uuid::new_v4())
            }
            "public_generation" => {
                changed
                    .authority
                    .as_mut()
                    .unwrap()
                    .expected_public_generation = 1
            }
            _ => unreachable!(),
        }
        assert!(repo.submit(&changed).is_err(), "{variant}");
        assert!(repo.get_run(changed.run_id).is_err(), "{variant}");
        assert!(public_id(&repo.get_project(fixture.id).unwrap()).is_none());
    }
    let _cleanup = submitted(&repo, &original);
    let plan = repo.get_plan(fixture.id, original.run_id).unwrap();
    assert_eq!(
        value(&repo.get_plan(fixture.id, original.run_id).unwrap()),
        value(&plan)
    );
    assert!(repo.get_plan(Uuid::new_v4(), original.run_id).is_err());
}

#[test]
fn private_prerequisite_success_does_not_publish_and_final_task_is_atomic() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let mut spec = run_spec(&fixture, &fixture.initial, None, 0);
    let prerequisite = task(&fixture.goal, "prepare");
    spec.tasks[0].depends_on = vec!["prepare".into()];
    spec.tasks[0].source_task = Some("prepare".into());
    spec.tasks.insert(0, prerequisite);
    let _cleanup = submitted(&repo, &spec);
    let first = claim(&repo, spec.run_id, "preparation");
    assert_eq!(first.token.task_key, "prepare");
    let private = output(&fixture, &first);
    repo.complete(&first.token, &private, &hash("private-completion"))
        .unwrap();
    assert!(public_id(&repo.get_project(fixture.id).unwrap()).is_none());
    let final_task = claim(&repo, spec.run_id, "final-owner");
    assert_eq!(final_task.source, private.source);
    let (published, _, _) = publish(&repo, &fixture, &final_task);
    let public = repo.get_project(fixture.id).unwrap();
    assert!(public_id(&public).is_some());
    assert_eq!(public_generation(&public), 1);
    assert_eq!(repo.get_run(spec.run_id).unwrap().state, "completed");
    assert_eq!(
        repo.get_run(spec.run_id).unwrap().tasks[1].output.as_ref(),
        Some(&published)
    );
}

#[test]
fn two_candidates_from_one_public_base_have_only_one_verified_public_commit() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let first = run_spec(&fixture, &fixture.initial, None, 0);
    let second = run_spec(&fixture, &fixture.initial, None, 0);
    let _first_cleanup = submitted(&repo, &first);
    let _second_cleanup = submitted(&repo, &second);
    let first_lease = claim(&repo, first.run_id, "first-candidate");
    let second_lease = claim(&repo, second.run_id, "second-candidate");
    let barrier = std::sync::Arc::new(Barrier::new(3));
    let mut handles = vec![];
    for lease in [first_lease, second_lease] {
        let candidate = output(&fixture, &lease);
        let evidence = evidence(&fixture.goal, &candidate);
        let repo = repo.clone();
        let barrier = barrier.clone();
        handles.push(thread::spawn(move || {
            barrier.wait();
            repo.complete_with_publication(
                &lease.token,
                &candidate,
                &hash(&format!("racing-completion:{}", lease.token.attempt_id)),
                Some(&evidence),
            )
        }));
    }
    barrier.wait();
    let outcomes: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    assert_eq!(outcomes.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(
        outcomes
            .iter()
            .filter_map(|r| r.as_ref().err())
            .next()
            .unwrap()
            .code,
        "PUBLIC_HEAD_CONFLICT"
    );
    let public = repo.get_project(fixture.id).unwrap();
    assert_eq!(public_generation(&public), 1);
    let commit = repo
        .get_commit(fixture.id, public_id(&public).unwrap())
        .unwrap();
    let commits = value(&commit).to_string();
    let accepted = outcomes.into_iter().find_map(Result::ok).unwrap();
    assert!(commits.contains(&accepted.source.sha256));
}

#[test]
fn missing_or_misbound_publication_evidence_keeps_the_previous_authority() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let spec = run_spec(&fixture, &fixture.initial, None, 0);
    let _cleanup = submitted(&repo, &spec);
    let lease = claim(&repo, spec.run_id, "checked-owner");
    let candidate = output(&fixture, &lease);
    let digest = hash("unaccepted-publication");
    let before = value(&repo.get_project(fixture.id).unwrap());
    assert_eq!(
        repo.complete(&lease.token, &candidate, &digest)
            .unwrap_err()
            .code,
        "PUBLICATION_EVIDENCE_REQUIRED"
    );
    for field in [
        "goal_revision_id",
        "acceptance_sha256",
        "precision_sha256",
        "source_sha256",
        "materialization",
        "step_geometry_sha256",
    ] {
        let mut data = value(&evidence(&fixture.goal, &candidate));
        data[field] = if field == "goal_revision_id" {
            json!(Uuid::new_v4())
        } else {
            json!(hash("foreign evidence"))
        };
        let changed: PublicationEvidence = serde_json::from_value(data).unwrap();
        assert!(
            repo.complete_with_publication(&lease.token, &candidate, &digest, Some(&changed))
                .is_err(),
            "{field}"
        );
        assert_eq!(
            value(&repo.get_project(fixture.id).unwrap()),
            before,
            "{field}"
        );
        assert_eq!(
            repo.get_run(spec.run_id).unwrap().tasks[0].state,
            "running",
            "{field}"
        );
    }
    publish(&repo, &fixture, &lease);
}

#[test]
fn goal_changes_revoke_old_execution_and_preserve_last_verified_commit() {
    let Some(repo) = repository() else { return };
    let mut fixture = fixture(&repo);
    let initial = run_spec(&fixture, &fixture.initial, None, 0);
    let _cleanup = submitted(&repo, &initial);
    let first = claim(&repo, initial.run_id, "first-publication");
    let (published, _, _) = publish(&repo, &fixture, &first);
    let before = repo.get_project(fixture.id).unwrap();
    let old_goal = fixture.goal.clone();
    let old_work = run_spec(&fixture, &published.source, public_id(&before), 1);
    let _old_cleanup = submitted(&repo, &old_work);
    let stale = claim(&repo, old_work.run_id, "old-goal-worker");
    let old_candidate = output(&fixture, &stale);
    let old_evidence = evidence(&fixture.goal, &old_candidate);
    fixture.goal = repo
        .update_goal(fixture.id, &update_request(Some(goal_id(&old_goal)), 1))
        .unwrap();
    assert!(repo
        .complete_with_publication(
            &stale.token,
            &old_candidate,
            &hash("old-goal-completion"),
            Some(&old_evidence)
        )
        .is_err());
    assert!(repo.heartbeat(&stale.token).is_err());
    assert_eq!(repo.get_run(old_work.run_id).unwrap().state, "cancelled");
    let after = repo.get_project(fixture.id).unwrap();
    assert_eq!(public_id(&after), public_id(&before));
    assert_eq!(public_generation(&after), 1);
    assert_eq!(
        value(&repo.get_goal(fixture.id, goal_id(&old_goal)).unwrap()),
        value(&old_goal)
    );
    let new_work = run_spec(&fixture, &published.source, public_id(&after), 1);
    let _new_cleanup = submitted(&repo, &new_work);
    let replacement = claim(&repo, new_work.run_id, "new-goal-worker");
    publish(&repo, &fixture, &replacement);
    assert_eq!(public_generation(&repo.get_project(fixture.id).unwrap()), 2);
}

#[test]
fn expired_attempt_cannot_publish_and_recovery_does_not_lose_the_public_base() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let spec = run_spec(&fixture, &fixture.initial, None, 0);
    let _cleanup = submitted(&repo, &spec);
    let stale = claim(&repo, spec.run_id, "expired-owner");
    let stale_output = output(&fixture, &stale);
    let stale_evidence = evidence(&fixture.goal, &stale_output);
    expire(&stale);
    assert_eq!(
        repo.complete_with_publication(
            &stale.token,
            &stale_output,
            &hash("expired-completion"),
            Some(&stale_evidence)
        )
        .unwrap_err()
        .code,
        "STALE_EXECUTION_LEASE"
    );
    assert!(public_id(&repo.get_project(fixture.id).unwrap()).is_none());
    repo.recover().unwrap();
    let mut db =
        Client::connect(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), NoTls).unwrap();
    db.execute(
        "UPDATE deep3d_runtime.tasks SET available_at=clock_timestamp() WHERE run_id=$1",
        &[&spec.run_id],
    )
    .unwrap();
    let replacement = claim(&repo, spec.run_id, "recovered-owner");
    assert_eq!(replacement.token.execution_epoch, 2);
    assert_eq!(replacement.source, fixture.initial);
    publish(&repo, &fixture, &replacement);
    let published = value(&repo.get_project(fixture.id).unwrap());
    assert!(repo
        .complete_with_publication(
            &stale.token,
            &stale_output,
            &hash("expired-completion"),
            Some(&stale_evidence)
        )
        .is_err());
    assert_eq!(value(&repo.get_project(fixture.id).unwrap()), published);
}

#[test]
fn publication_checks_expiry_after_waiting_for_the_run_authority_lock() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let spec = run_spec(&fixture, &fixture.initial, None, 0);
    let _cleanup = submitted(&repo, &spec);
    let lease = claim(&repo, spec.run_id, "waiting-owner");
    let candidate = output(&fixture, &lease);
    let evidence = evidence(&fixture.goal, &candidate);
    let mut db =
        Client::connect(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), NoTls).unwrap();
    let mut held = db.transaction().unwrap();
    held.query_one(
        "SELECT run_id FROM deep3d_runtime.runs WHERE run_id=$1 FOR UPDATE",
        &[&spec.run_id],
    )
    .unwrap();
    held.execute("UPDATE deep3d_runtime.attempts SET lease_until=clock_timestamp()+interval '400 milliseconds' WHERE run_id=$1 AND task_key=$2 AND attempt_id=$3", &[&spec.run_id,&lease.token.task_key,&lease.token.attempt_id]).unwrap();
    let completing_repo = repo.clone();
    let pending = thread::spawn(move || {
        completing_repo.complete_with_publication(
            &lease.token,
            &candidate,
            &hash("waited-completion"),
            Some(&evidence),
        )
    });
    thread::sleep(Duration::from_millis(650));
    held.commit().unwrap();
    assert_eq!(
        pending.join().unwrap().unwrap_err().code,
        "STALE_EXECUTION_LEASE"
    );
    assert!(public_id(&repo.get_project(fixture.id).unwrap()).is_none());
    assert_eq!(repo.get_run(spec.run_id).unwrap().tasks[0].state, "running");
}

#[test]
fn successful_completion_replay_after_later_goal_and_commit_cannot_publish_twice_or_roll_back() {
    let Some(repo) = repository() else { return };
    let fixture = fixture(&repo);
    let first_spec = run_spec(&fixture, &fixture.initial, None, 0);
    let _first_cleanup = submitted(&repo, &first_spec);
    let first_lease = claim(&repo, first_spec.run_id, "first-owner");
    let (first_output, first_evidence, first_digest) = publish(&repo, &fixture, &first_lease);
    let first_public = repo.get_project(fixture.id).unwrap();
    let first_commit = public_id(&first_public).unwrap();
    let historical = repo.get_commit(fixture.id, first_commit).unwrap();
    let second_spec = run_spec(&fixture, &first_output.source, Some(first_commit), 1);
    let _second_cleanup = submitted(&repo, &second_spec);
    let second_lease = claim(&repo, second_spec.run_id, "second-owner");
    publish(&repo, &fixture, &second_lease);
    repo.update_goal(fixture.id, &update_request(Some(goal_id(&fixture.goal)), 1))
        .unwrap();
    let before = value(&repo.get_project(fixture.id).unwrap());
    assert_eq!(public_generation(&repo.get_project(fixture.id).unwrap()), 2);
    expire(&first_lease);
    assert_eq!(
        repo.completion(&first_lease.token, &first_digest).unwrap(),
        Some(first_output.clone())
    );
    assert_eq!(
        repo.complete_with_publication(
            &first_lease.token,
            &first_output,
            &first_digest,
            Some(&first_evidence)
        )
        .unwrap(),
        first_output
    );
    assert_eq!(value(&repo.get_project(fixture.id).unwrap()), before);
    assert_eq!(
        value(&repo.get_commit(fixture.id, first_commit).unwrap()),
        value(&historical)
    );
    assert_eq!(
        repo.complete_with_publication(
            &first_lease.token,
            &first_output,
            &hash("different-completion"),
            Some(&first_evidence)
        )
        .unwrap_err()
        .code,
        "IDEMPOTENCY_CONFLICT"
    );
    let restarted =
        Repository::new(&std::env::var("DEEP3D_TEST_DATABASE_URL").unwrap(), 30).unwrap();
    assert_eq!(value(&restarted.get_project(fixture.id).unwrap()), before);
    assert_eq!(
        value(&restarted.get_commit(fixture.id, first_commit).unwrap()),
        value(&historical)
    );
}

#[test]
fn unresolved_required_requirements_are_retained_and_cannot_be_declared_published() {
    let Some(repo) = repository() else { return };
    for state in ["pending_confirmation", "unverifiable", "conflicting"] {
        let id = Uuid::new_v4();
        let initial = source(&format!("unresolved:{id}"), 0);
        repo.register_project(id, &initial, Uuid::new_v4()).unwrap();
        let mut request = update_request(None, 0);
        let mut data = value(&request.definition);
        data["unresolved"] = json!([{
            "id":"external-strength",
            "description":"No applicable independent strength detector",
            "status":state,
            "required":true,
            "source_refs":["operator:protocol-fixture"],
        }]);
        request.definition = serde_json::from_value(data).unwrap();
        let goal = repo.update_goal(id, &request).unwrap();
        assert_eq!(
            value(&repo.get_goal(id, goal_id(&goal)).unwrap())["definition"]["unresolved"][0]
                ["status"],
            state
        );
        let fixture = Fixture { id, initial, goal };
        let spec = run_spec(&fixture, &fixture.initial, None, 0);
        match repo.submit(&spec) {
            Err(error) => assert_eq!(error.code, "UNRESOLVED_GOAL"),
            Ok(_) => {
                let _cleanup = Cleanup(repo.clone(), spec.run_id);
                let lease = claim(&repo, spec.run_id, "unresolved-owner");
                let candidate = output(&fixture, &lease);
                let evidence = evidence(&fixture.goal, &candidate);
                assert_eq!(
                    repo.complete_with_publication(
                        &lease.token,
                        &candidate,
                        &hash("unresolved-completion"),
                        Some(&evidence)
                    )
                    .unwrap_err()
                    .code,
                    "UNRESOLVED_GOAL"
                );
            }
        }
        assert!(public_id(&repo.get_project(id).unwrap()).is_none());
        assert_eq!(public_generation(&repo.get_project(id).unwrap()), 0);
    }
}

#[test]
fn project_goal_plan_commit_and_attempt_references_cannot_cross_scope() {
    let Some(repo) = repository() else { return };
    let first = fixture(&repo);
    let second = fixture(&repo);
    assert!(repo.get_goal(second.id, goal_id(&first.goal)).is_err());
    let first_spec = run_spec(&first, &first.initial, None, 0);
    let second_spec = run_spec(&second, &second.initial, None, 0);
    let _first_cleanup = submitted(&repo, &first_spec);
    let _second_cleanup = submitted(&repo, &second_spec);
    assert!(repo.get_plan(second.id, first_spec.run_id).is_err());
    let first_lease = claim(&repo, first_spec.run_id, "first-scope-owner");
    let second_lease = claim(&repo, second_spec.run_id, "second-scope-owner");
    let mut foreign_token = first_lease.token.clone();
    foreign_token.run_id = second_lease.token.run_id;
    assert!(repo.heartbeat(&foreign_token).is_err());
    let foreign_output = output(&first, &first_lease);
    let foreign_evidence = evidence(&first.goal, &foreign_output);
    assert!(repo
        .complete_with_publication(
            &second_lease.token,
            &foreign_output,
            &hash("foreign-output"),
            Some(&foreign_evidence)
        )
        .is_err());
    assert!(public_id(&repo.get_project(second.id).unwrap()).is_none());
    publish(&repo, &first, &first_lease);
    let committed = public_id(&repo.get_project(first.id).unwrap()).unwrap();
    assert!(repo.get_commit(second.id, committed).is_err());
    assert!(public_id(&repo.get_project(second.id).unwrap()).is_none());
}
