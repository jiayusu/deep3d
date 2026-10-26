use super::*;
use core_domain::{Command, ScalarValue, TRANSACTION_SCHEMA};
use serde_json::json;
use std::sync::{Arc, Barrier};

fn fixture() -> (tempfile::TempDir, ProjectStore, Snapshot) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("flange.deep3d");
    let snapshot = ProjectStore::create(&path, &Project::new_flange("Fixture").unwrap()).unwrap();
    let store = ProjectStore::open(path).unwrap();
    (directory, store, snapshot)
}

fn edit(snapshot: &Snapshot, thickness: f64) -> Transaction {
    Transaction {
        id: Uuid::new_v4(),
        schema_version: TRANSACTION_SCHEMA.into(),
        base_revision: snapshot.revision.id,
        expected_generation: snapshot.head.generation,
        message: format!("Set thickness to {thickness}"),
        commands: vec![Command::ParameterSet {
            id: snapshot.project.parameter_id("thickness_mm").unwrap(),
            value: ScalarValue::LengthMm(thickness),
        }],
    }
}

#[test]
fn reopen_edit_and_history_preserve_old_parameters_and_stable_ids() {
    let (_directory, store, initial) = fixture();
    let next = store.commit(&edit(&initial, 16.0)).unwrap();
    let reopened = ProjectStore::open(store.path()).unwrap();
    assert_eq!(
        reopened
            .head()
            .unwrap()
            .project
            .flange_spec()
            .unwrap()
            .thickness_mm,
        16.0
    );
    assert_eq!(
        reopened
            .read_revision(initial.revision.id)
            .unwrap()
            .project
            .flange_spec()
            .unwrap()
            .thickness_mm,
        12.0
    );
    assert_eq!(
        initial.project.features.keys().collect::<Vec<_>>(),
        next.project.features.keys().collect::<Vec<_>>()
    );
    assert_eq!(next.revision.parent, Some(initial.revision.id));
    assert_eq!(next.head.generation, 1);
    assert_eq!(reopened.history().unwrap().len(), 2);
}

#[test]
fn create_never_overwrites_and_invalid_edit_keeps_identical_file_bytes() {
    let (_directory, store, initial) = fixture();
    let original = fs::read(store.path()).unwrap();
    assert_eq!(
        ProjectStore::create(store.path(), &initial.project)
            .unwrap_err()
            .code,
        "PROJECT_EXISTS"
    );
    assert!(store.commit(&edit(&initial, -1.0)).is_err());
    assert_eq!(fs::read(store.path()).unwrap(), original);
    assert_eq!(store.head().unwrap().head, initial.head);
}

#[test]
fn stale_head_or_generation_fails_and_successful_transaction_retries_are_idempotent() {
    let (_directory, store, initial) = fixture();
    let tx = edit(&initial, 16.0);
    let next = store.commit(&tx).unwrap();
    assert_eq!(store.commit(&tx).unwrap().revision.id, next.revision.id);
    assert_eq!(store.history().unwrap().len(), 2);
    let mut changed = tx.clone();
    changed.message = "different content".into();
    assert_eq!(
        store.commit(&changed).unwrap_err().code,
        "IDEMPOTENCY_CONFLICT"
    );
    assert_eq!(
        store.commit(&edit(&initial, 20.0)).unwrap_err().code,
        "REVISION_CONFLICT"
    );
    let mut wrong_generation = edit(&next, 20.0);
    wrong_generation.expected_generation = 0;
    assert_eq!(
        store.commit(&wrong_generation).unwrap_err().code,
        "REVISION_CONFLICT"
    );
}

#[test]
fn active_writer_lock_is_explicit_and_does_not_block_forever() {
    let (_directory, store, initial) = fixture();
    let lock = acquire_lock(store.path()).unwrap();
    assert_eq!(
        store.commit(&edit(&initial, 16.0)).unwrap_err().code,
        "PROJECT_BUSY"
    );
    drop(lock);
    assert!(store.commit(&edit(&initial, 16.0)).is_ok());
}

#[test]
fn concurrent_writers_have_only_one_successful_cas() {
    let (_directory, store, initial) = fixture();
    let barrier = Arc::new(Barrier::new(3));
    let mut handles = vec![];
    for thickness in [16.0, 20.0] {
        let store = store.clone();
        let tx = edit(&initial, thickness);
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            barrier.wait();
            store.commit(&tx)
        }));
    }
    barrier.wait();
    let outcomes: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    assert_eq!(outcomes.iter().filter(|r| r.is_ok()).count(), 1);
    assert!(outcomes
        .iter()
        .filter_map(|r| r.as_ref().err())
        .all(|e| e.code == "PROJECT_BUSY" || e.code == "REVISION_CONFLICT"));
    assert_eq!(
        ProjectStore::open(store.path())
            .unwrap()
            .history()
            .unwrap()
            .len(),
        2
    );
}

fn write_entries(path: &Path, entries: Vec<(String, Vec<u8>)>) {
    let mut zip = ZipWriter::new(File::create(path).unwrap());
    for (name, bytes) in entries {
        zip.start_file(
            name,
            SimpleFileOptions::default().compression_method(CompressionMethod::Stored),
        )
        .unwrap();
        zip.write_all(&bytes).unwrap();
    }
    zip.finish().unwrap();
}

fn entries(path: &Path) -> Vec<(String, Vec<u8>)> {
    let mut archive = ZipArchive::new(File::open(path).unwrap()).unwrap();
    (0..archive.len())
        .map(|i| {
            let mut entry = archive.by_index(i).unwrap();
            let name = entry.name().to_owned();
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes).unwrap();
            (name, bytes)
        })
        .collect()
}

#[test]
fn changed_object_bytes_missing_objects_and_invalid_zip_fail_closed() {
    for mode in ["changed", "missing", "badzip"] {
        let (_directory, store, _) = fixture();
        let mut files = entries(store.path());
        match mode {
            "changed" => files[1].1.push(b' '),
            "missing" => {
                files.pop();
            }
            _ => {
                fs::write(store.path(), b"not ZIP").unwrap();
            }
        }
        if mode != "badzip" {
            write_entries(store.path(), files);
        }
        assert!(ProjectStore::open(store.path()).is_err(), "{mode}");
    }
}

#[test]
fn duplicate_paths_and_traversal_are_rejected_without_extraction() {
    for unsafe_name in ["../escaped.txt", "/absolute", "objects/../../escaped"] {
        let (directory, store, _) = fixture();
        let mut files = entries(store.path());
        files.push((unsafe_name.into(), b"bad".to_vec()));
        write_entries(store.path(), files);
        assert!(ProjectStore::open(store.path()).is_err());
        assert!(!directory.path().join("escaped.txt").exists());
    }
}

#[test]
fn duplicate_zip_names_and_crc_failures_are_rejected() {
    let (_directory, store, _) = fixture();
    let mut files = entries(store.path());
    files.push(("manifest.jsox".into(), b"{}".to_vec()));
    write_entries(store.path(), files);
    let mut raw = fs::read(store.path()).unwrap();
    let needle = b"manifest.jsox";
    for start in 0..=raw.len() - needle.len() {
        if &raw[start..start + needle.len()] == needle {
            raw[start..start + needle.len()].copy_from_slice(b"manifest.json");
        }
    }
    fs::write(store.path(), raw).unwrap();
    assert_eq!(
        ProjectStore::open(store.path()).unwrap_err().code,
        "PROJECT_CORRUPT"
    );
    let (_directory, store, _) = fixture();
    let position = {
        let mut zip = ZipArchive::new(File::open(store.path()).unwrap()).unwrap();
        let position = zip.by_index(1).unwrap().data_start();
        position
    };
    let mut raw = fs::read(store.path()).unwrap();
    raw[position as usize] ^= 1;
    fs::write(store.path(), raw).unwrap();
    assert_eq!(
        ProjectStore::open(store.path()).unwrap_err().code,
        "PROJECT_CORRUPT"
    );
}

#[test]
fn unknown_schema_and_oversized_container_or_metadata_are_rejected() {
    let (_directory, store, _) = fixture();
    let mut files = entries(store.path());
    let mut manifest: serde_json::Value = serde_json::from_slice(&files[0].1).unwrap();
    manifest["schema_version"] = json!("deep3d.project/99");
    files[0].1 = canonical_bytes(&manifest).unwrap();
    write_entries(store.path(), files);
    assert_eq!(
        ProjectStore::open(store.path()).unwrap_err().code,
        "SCHEMA_UNSUPPORTED"
    );
    let (_directory, store, _) = fixture();
    OpenOptions::new()
        .write(true)
        .open(store.path())
        .unwrap()
        .set_len(MAX_ARCHIVE_BYTES + 1)
        .unwrap();
    assert_eq!(
        ProjectStore::open(store.path()).unwrap_err().code,
        "PROJECT_TOO_LARGE"
    );
    let (_directory, store, _) = fixture();
    write_entries(
        store.path(),
        vec![(
            "manifest.json".into(),
            vec![b' '; MAX_METADATA_BYTES as usize + 1],
        )],
    );
    assert_eq!(
        ProjectStore::open(store.path()).unwrap_err().code,
        "PROJECT_TOO_LARGE"
    );
}

// Protocol fixtures only: Rust does not perform geometry or pretend these
// bytes are CAD evidence. Real OCCT evidence is tested by the CLI integration.
fn metadata_materialization(snapshot: &Snapshot) -> (Materialization, BTreeMap<String, Vec<u8>>) {
    let plan = snapshot.project.compile(snapshot.revision.id).unwrap();
    let plan_bytes = canonical_bytes(&plan).unwrap();
    let plan_hash = digest_bytes(&plan_bytes);
    let mut blobs = BTreeMap::from([(plan_hash.clone(), plan_bytes)]);
    let mut files = BTreeMap::new();
    let mut add = |name: String, bytes: Vec<u8>| {
        let hash = digest_bytes(&bytes);
        files.insert(
            name,
            ArtifactRef {
                sha256: hash.clone(),
                size_bytes: bytes.len() as u64,
            },
        );
        blobs.insert(hash.clone(), bytes);
        hash
    };
    add("plan.json".into(), serde_json::to_vec(&plan).unwrap());
    add(
        "goal.json".into(),
        serde_json::to_vec(&plan.acceptance).unwrap(),
    );
    add(
        "precision.json".into(),
        serde_json::to_vec(&plan.precision).unwrap(),
    );
    add(
        "build.json".into(),
        serde_json::to_vec(&json!({"status":"pass"})).unwrap(),
    );
    let final_feature = plan.outputs.values().next().unwrap();
    let final_geometry_hash = digest_bytes(format!("protocol-fixture:{final_feature}").as_bytes());
    let step_hash = digest_bytes(b"protocol-fixture-step");
    let validator_hash = digest_bytes(b"protocol-fixture-verifier");
    for (name, stage) in [("verify-brep.json", "native"), ("verify-step.json", "step")] {
        add(
            name.into(),
            serde_json::to_vec(&json!({
                "schema_version":"deep3d.flange-verification/1", "status":"pass", "stage":stage,
                "expected_spec":plan.acceptance,"precision_profile":plan.precision,
            "input_artifact_sha256":if stage == "native" { &final_geometry_hash } else { &step_hash },
            "validator_id":"deep3d.analytic-flange@1", "validator_source_sha256":validator_hash,
                "checks":[{"id":"test.metadata_protocol_only","status":"pass"}],
            }))
            .unwrap(),
        );
    }
    let feature_outputs: BTreeMap<_, _> = plan
        .features
        .iter()
        .map(|f| {
            (
                f.id,
                add(
                    format!("features/{}.brep", f.id),
                    format!("protocol-fixture:{}", f.id).into_bytes(),
                ),
            )
        })
        .collect();
    let node_outputs: BTreeMap<_, _> = plan
        .outputs
        .iter()
        .map(|(node, feature)| (*node, feature_outputs[feature].clone()))
        .collect();
    let final_bytes =
        format!("protocol-fixture:{}", plan.outputs.values().next().unwrap()).into_bytes();
    add("flange.brep".into(), final_bytes);
    add("flange.step".into(), b"protocol-fixture-step".to_vec());
    (
        Materialization {
            schema_version: MATERIALIZATION_SCHEMA.into(),
            project_id: plan.project_id,
            revision_id: plan.revision_id,
            plan_hash,
            engine: BTreeMap::from([
                ("test_boundary".into(), "protocol_only".into()),
                ("verifier_source_sha256".into(), validator_hash),
            ]),
            files,
            feature_outputs,
            node_outputs,
        },
        blobs,
    )
}

#[test]
fn materializations_are_self_contained_and_can_attach_to_an_old_revision() {
    let (_directory, store, original) = fixture();
    let newer = store.commit(&edit(&original, 16.0)).unwrap();
    let (record, blobs) = metadata_materialization(&original);
    let hash = store
        .attach_materialization(record.clone(), blobs.clone())
        .unwrap();
    assert_eq!(
        store.attach_materialization(record.clone(), blobs).unwrap(),
        hash
    );
    assert_eq!(store.head().unwrap().head, newer.head);
    assert!(store
        .materializations(newer.revision.id)
        .unwrap()
        .is_empty());
    let reopened = ProjectStore::open(store.path()).unwrap();
    assert_eq!(
        reopened.materializations(original.revision.id).unwrap()[0].1,
        record
    );
    let artifact = &record.files["flange.step"];
    assert_eq!(
        digest_bytes(&reopened.read_blob(&artifact.sha256).unwrap()),
        artifact.sha256
    );
}

#[test]
fn foreign_stale_failed_or_incomplete_evidence_never_changes_package() {
    for mutation in [
        "foreign",
        "node",
        "missing",
        "hash",
        "failed",
        "binding",
        "validator",
    ] {
        let (_directory, store, initial) = fixture();
        let before = fs::read(store.path()).unwrap();
        let (mut record, mut blobs) = metadata_materialization(&initial);
        match mutation {
            "foreign" => record.project_id = Uuid::new_v4(),
            "node" => {
                record.node_outputs.clear();
            }
            "missing" => {
                record.files.remove("verify-step.json");
            }
            "hash" => {
                let hash = record.files["flange.step"].sha256.clone();
                blobs.get_mut(&hash).unwrap().push(b'x');
            }
            "failed" | "binding" | "validator" => {
                let reference = record.files.get_mut("verify-step.json").unwrap();
                let previous = blobs.remove(&reference.sha256).unwrap();
                let mut value: serde_json::Value = serde_json::from_slice(&previous).unwrap();
                match mutation {
                    "failed" => value["status"] = json!("fail"),
                    "binding" => {
                        value["input_artifact_sha256"] = json!(digest_bytes(b"other-geometry"))
                    }
                    "validator" => {
                        value["validator_source_sha256"] = json!(digest_bytes(b"other-verifier"))
                    }
                    _ => unreachable!(),
                }
                let bytes = serde_json::to_vec(&value).unwrap();
                reference.sha256 = digest_bytes(&bytes);
                reference.size_bytes = bytes.len() as u64;
                blobs.insert(reference.sha256.clone(), bytes);
            }
            _ => unreachable!(),
        }
        assert!(
            store.attach_materialization(record, blobs).is_err(),
            "{mutation}"
        );
        assert_eq!(fs::read(store.path()).unwrap(), before, "{mutation}");
    }
}
