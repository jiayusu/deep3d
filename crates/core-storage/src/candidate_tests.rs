use super::*;
use core_domain::{Command, ScalarValue, TRANSACTION_SCHEMA};
use serde_json::json;

fn fixture() -> (tempfile::TempDir, ProjectStore, Snapshot) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("source.deep3d");
    let initial =
        ProjectStore::create(&path, &Project::new_flange("Candidate fixture").unwrap()).unwrap();
    let source = ProjectStore::open(path).unwrap();
    (directory, source, initial)
}

fn edit(snapshot: &Snapshot, thickness: f64) -> Transaction {
    Transaction {
        id: Uuid::new_v4(),
        schema_version: TRANSACTION_SCHEMA.into(),
        base_revision: snapshot.revision.id,
        expected_generation: snapshot.head.generation,
        message: format!("Candidate thickness {thickness}"),
        commands: vec![Command::ParameterSet {
            id: snapshot.project.parameter_id("thickness_mm").unwrap(),
            value: ScalarValue::LengthMm(thickness),
        }],
    }
}

fn materialize(candidate: &ProjectStore, snapshot: &Snapshot) -> String {
    let (record, blobs) = super::tests::metadata_materialization(snapshot);
    candidate.attach_materialization(record, blobs).unwrap()
}

fn write_unchecked(path: &Path, state: &State) {
    let mut writer = ZipWriter::new(File::create(path).unwrap());
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Stored);
    writer.start_file("manifest.json", options).unwrap();
    writer
        .write_all(&canonical_bytes(&state.manifest).unwrap())
        .unwrap();
    for (hash, bytes) in &state.objects {
        writer
            .start_file(format!("objects/{}", &hash[7..]), options)
            .unwrap();
        writer.write_all(bytes).unwrap();
    }
    writer.finish().unwrap();
}

#[test]
fn candidate_is_one_private_revision_and_source_bytes_never_change() {
    let (directory, source, initial) = fixture();
    let before = fs::read(source.path()).unwrap();
    let transaction = edit(&initial, 16.0);
    let path = directory.path().join("candidate.deep3d");
    let prepared = source.prepare_candidate(&transaction, &path).unwrap();
    assert_eq!(prepared.project.project_id, initial.project.project_id);
    assert_eq!(prepared.revision.parent, Some(initial.revision.id));
    assert_eq!(prepared.head.generation, initial.head.generation + 1);
    assert_eq!(prepared.revision.transaction_id, Some(transaction.id));
    assert_eq!(prepared.project.flange_spec().unwrap().thickness_mm, 16.0);
    let candidate = ProjectStore::open(path).unwrap();
    assert_eq!(candidate.history().unwrap().len(), 2);
    assert_eq!(candidate.history().unwrap()[0], initial.revision);
    assert_eq!(source.head().unwrap().head, initial.head);
    assert_eq!(fs::read(source.path()).unwrap(), before);
}

#[test]
fn candidate_rejects_invalid_stale_existing_or_source_output_without_writing() {
    let (directory, source, initial) = fixture();
    let before = fs::read(source.path()).unwrap();
    let path = directory.path().join("candidate.deep3d");
    assert!(source
        .prepare_candidate(&edit(&initial, -1.0), &path)
        .is_err());
    assert!(!path.exists());
    let mut stale = edit(&initial, 16.0);
    stale.expected_generation += 1;
    assert_eq!(
        source.prepare_candidate(&stale, &path).unwrap_err().code,
        "REVISION_CONFLICT"
    );
    assert!(!path.exists());
    assert_eq!(
        source
            .prepare_candidate(&edit(&initial, 16.0), source.path())
            .unwrap_err()
            .code,
        "CANDIDATE_INVALID"
    );
    fs::write(&path, b"existing output").unwrap();
    assert_eq!(
        source
            .prepare_candidate(&edit(&initial, 16.0), &path)
            .unwrap_err()
            .code,
        "PROJECT_EXISTS"
    );
    assert_eq!(fs::read(&path).unwrap(), b"existing output");
    assert_eq!(fs::read(source.path()).unwrap(), before);
    assert!(source
        .prepare_candidate(
            &edit(&initial, 16.0),
            directory.path().join("missing/candidate.deep3d"),
        )
        .is_err());
}

#[test]
fn source_writer_lock_is_respected_during_candidate_creation_and_adoption() {
    let (directory, source, initial) = fixture();
    let path = directory.path().join("candidate.deep3d");
    let lock = acquire_lock(source.path()).unwrap();
    assert_eq!(
        source
            .prepare_candidate(&edit(&initial, 16.0), &path)
            .unwrap_err()
            .code,
        "PROJECT_BUSY"
    );
    drop(lock);
    let prepared = source
        .prepare_candidate(&edit(&initial, 16.0), &path)
        .unwrap();
    let candidate = ProjectStore::open(&path).unwrap();
    let hash = materialize(&candidate, &prepared);
    let lock = acquire_lock(source.path()).unwrap();
    assert_eq!(
        source
            .adopt_candidate(&path, initial.head, &hash)
            .unwrap_err()
            .code,
        "PROJECT_BUSY"
    );
    drop(lock);
}

#[test]
fn verified_candidate_adoption_is_idempotent_even_after_more_source_edits() {
    let (directory, source, initial) = fixture();
    let path = directory.path().join("candidate.deep3d");
    let prepared = source
        .prepare_candidate(&edit(&initial, 16.0), &path)
        .unwrap();
    let candidate = ProjectStore::open(&path).unwrap();
    let hash = materialize(&candidate, &prepared);
    let accepted = source
        .adopt_candidate(&path, initial.head.clone(), &hash)
        .unwrap();
    assert_eq!(accepted.revision, prepared.revision);
    assert_eq!(accepted.head, prepared.head);
    assert_eq!(
        source.materializations(accepted.revision.id).unwrap().len(),
        1
    );
    let before = fs::read(source.path()).unwrap();
    let retried = source
        .adopt_candidate(&path, initial.head.clone(), &hash)
        .unwrap();
    assert_eq!(retried.revision, accepted.revision);
    assert_eq!(fs::read(source.path()).unwrap(), before);
    let newer = source.commit(&edit(&accepted, 20.0)).unwrap();
    let before = fs::read(source.path()).unwrap();
    let retried = source.adopt_candidate(&path, initial.head, &hash).unwrap();
    assert_eq!(retried.revision, accepted.revision);
    assert_eq!(retried.head, newer.head);
    assert_eq!(fs::read(source.path()).unwrap(), before);
}

#[test]
fn source_edit_after_fork_prevents_adoption_and_keeps_current_bytes() {
    let (directory, source, initial) = fixture();
    let path = directory.path().join("candidate.deep3d");
    let prepared = source
        .prepare_candidate(&edit(&initial, 16.0), &path)
        .unwrap();
    let candidate = ProjectStore::open(&path).unwrap();
    let hash = materialize(&candidate, &prepared);
    let manual = source.commit(&edit(&initial, 20.0)).unwrap();
    let before = fs::read(source.path()).unwrap();
    assert_eq!(
        source
            .adopt_candidate(&path, initial.head, &hash)
            .unwrap_err()
            .code,
        "REVISION_CONFLICT"
    );
    assert_eq!(source.head().unwrap().revision, manual.revision);
    assert_eq!(fs::read(source.path()).unwrap(), before);
}

#[test]
fn source_materializations_added_after_fork_survive_candidate_merge() {
    let (directory, source, initial) = fixture();
    let path = directory.path().join("candidate.deep3d");
    let prepared = source
        .prepare_candidate(&edit(&initial, 16.0), &path)
        .unwrap();
    let candidate = ProjectStore::open(&path).unwrap();
    let candidate_hash = materialize(&candidate, &prepared);
    let source_hash = materialize(&source, &initial);
    assert_eq!(source.head().unwrap().head, initial.head);
    assert!(candidate
        .materializations(initial.revision.id)
        .unwrap()
        .is_empty());
    let (source_record, _) = super::tests::metadata_materialization(&initial);
    source
        .adopt_candidate(&path, initial.head, &candidate_hash)
        .unwrap();
    let reopened = ProjectStore::open(source.path()).unwrap();
    assert_eq!(
        reopened.materializations(initial.revision.id).unwrap(),
        vec![(source_hash, source_record.clone())]
    );
    assert_eq!(
        reopened.materializations(prepared.revision.id).unwrap()[0].0,
        candidate_hash
    );
    for reference in source_record.files.values() {
        assert_eq!(
            digest_bytes(&reopened.read_blob(&reference.sha256).unwrap()),
            reference.sha256
        );
    }
}

#[test]
fn missing_old_revision_foreign_or_multi_revision_candidates_are_rejected() {
    let (directory, source, initial) = fixture();
    let source_hash = materialize(&source, &initial);
    let before = fs::read(source.path()).unwrap();
    let path = directory.path().join("candidate.deep3d");
    let prepared = source
        .prepare_candidate(&edit(&initial, 16.0), &path)
        .unwrap();
    let candidate = ProjectStore::open(&path).unwrap();
    assert_eq!(
        source
            .adopt_candidate(&path, initial.head.clone(), &digest_bytes(b"missing"))
            .unwrap_err()
            .code,
        "MATERIALIZATION_NOT_FOUND"
    );
    assert_eq!(
        source
            .adopt_candidate(&path, initial.head.clone(), &source_hash)
            .unwrap_err()
            .code,
        "CANDIDATE_INVALID"
    );
    assert_eq!(
        source
            .adopt_candidate(source.path(), initial.head.clone(), &source_hash)
            .unwrap_err()
            .code,
        "CANDIDATE_INVALID"
    );
    let unrelated_path = directory.path().join("unrelated.deep3d");
    let unrelated = ProjectStore::create(
        &unrelated_path,
        &Project::new_flange("Foreign project").unwrap(),
    )
    .unwrap();
    let unrelated_store = ProjectStore::open(&unrelated_path).unwrap();
    let unrelated_hash = materialize(&unrelated_store, &unrelated);
    assert_eq!(
        source
            .adopt_candidate(&unrelated_path, initial.head.clone(), &unrelated_hash)
            .unwrap_err()
            .code,
        "CANDIDATE_INVALID"
    );
    let second = candidate.commit(&edit(&prepared, 20.0)).unwrap();
    let second_hash = materialize(&candidate, &second);
    assert_eq!(
        source
            .adopt_candidate(&path, initial.head, &second_hash)
            .unwrap_err()
            .code,
        "CANDIDATE_INVALID"
    );
    assert_eq!(fs::read(source.path()).unwrap(), before);
}

#[test]
fn altered_source_history_in_an_otherwise_valid_candidate_is_rejected() {
    let (directory, source, initial) = fixture();
    let path = directory.path().join("candidate.deep3d");
    let prepared = source
        .prepare_candidate(&edit(&initial, 16.0), &path)
        .unwrap();
    let candidate = ProjectStore::open(&path).unwrap();
    let hash = materialize(&candidate, &prepared);
    let mut state = load(&path).unwrap();
    state
        .manifest
        .revisions
        .get_mut(&initial.revision.id)
        .unwrap()
        .message = "Changed immutable history".into();
    save(&path, &state, false).unwrap();
    let before = fs::read(source.path()).unwrap();
    assert_eq!(
        source
            .adopt_candidate(&path, initial.head, &hash)
            .unwrap_err()
            .code,
        "CANDIDATE_INVALID"
    );
    assert_eq!(fs::read(source.path()).unwrap(), before);
}

#[test]
fn failed_or_foreign_evidence_and_corrupted_objects_never_publish() {
    for mutation in ["failed", "foreign", "changed_object"] {
        let (directory, source, initial) = fixture();
        let path = directory.path().join("candidate.deep3d");
        let prepared = source
            .prepare_candidate(&edit(&initial, 16.0), &path)
            .unwrap();
        let mut state = load(&path).unwrap();
        let (mut record, mut blobs) = super::tests::metadata_materialization(&prepared);
        match mutation {
            "foreign" => record.project_id = Uuid::new_v4(),
            "failed" => {
                let reference = record.files.get_mut("verify-step.json").unwrap();
                let bytes = blobs.remove(&reference.sha256).unwrap();
                let mut report: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
                report["status"] = json!("fail");
                let bytes = serde_json::to_vec(&report).unwrap();
                reference.sha256 = digest_bytes(&bytes);
                reference.size_bytes = bytes.len() as u64;
                blobs.insert(reference.sha256.clone(), bytes);
            }
            "changed_object" => {
                blobs
                    .get_mut(&record.files["flange.step"].sha256)
                    .unwrap()
                    .push(b'!');
            }
            _ => unreachable!(),
        }
        let bytes = canonical_bytes(&record).unwrap();
        let hash = digest_bytes(&bytes);
        state.objects.extend(blobs);
        state.objects.insert(hash.clone(), bytes);
        state.manifest.materializations.insert(hash.clone());
        write_unchecked(&path, &state);
        let before = fs::read(source.path()).unwrap();
        assert!(
            source.adopt_candidate(&path, initial.head, &hash).is_err(),
            "{mutation}"
        );
        assert_eq!(fs::read(source.path()).unwrap(), before, "{mutation}");
    }
}

#[test]
fn reused_transaction_identity_with_different_content_is_rejected() {
    let (directory, source, initial) = fixture();
    let first_path = directory.path().join("first.deep3d");
    let second_path = directory.path().join("second.deep3d");
    let first_tx = edit(&initial, 16.0);
    let mut second_tx = edit(&initial, 20.0);
    second_tx.id = first_tx.id;
    let first = source.prepare_candidate(&first_tx, &first_path).unwrap();
    let second = source.prepare_candidate(&second_tx, &second_path).unwrap();
    let first_hash = materialize(&ProjectStore::open(&first_path).unwrap(), &first);
    let second_hash = materialize(&ProjectStore::open(&second_path).unwrap(), &second);
    source
        .adopt_candidate(&first_path, initial.head.clone(), &first_hash)
        .unwrap();
    let before = fs::read(source.path()).unwrap();
    assert_eq!(
        source
            .adopt_candidate(&second_path, initial.head, &second_hash)
            .unwrap_err()
            .code,
        "IDEMPOTENCY_CONFLICT"
    );
    assert_eq!(fs::read(source.path()).unwrap(), before);
}
