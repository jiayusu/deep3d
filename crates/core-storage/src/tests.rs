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
pub(super) fn metadata_materialization(
    snapshot: &Snapshot,
) -> (Materialization, BTreeMap<String, Vec<u8>>) {
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

fn replace_metadata_artifact(
    record: &mut Materialization,
    blobs: &mut BTreeMap<String, Vec<u8>>,
    name: &str,
    bytes: Vec<u8>,
) -> String {
    let hash = digest_bytes(&bytes);
    record.files.insert(
        name.into(),
        ArtifactRef {
            sha256: hash.clone(),
            size_bytes: bytes.len() as u64,
        },
    );
    blobs.insert(hash.clone(), bytes);
    hash
}

// Like metadata_materialization, these synthetic bytes test the storage protocol,
// not geometry validity. The real rigid-stack source supplies shared part identities.
fn mechanical_metadata_materialization(
    snapshot: &Snapshot,
) -> (Materialization, BTreeMap<String, Vec<u8>>) {
    let plan = snapshot.project.compile(snapshot.revision.id).unwrap();
    assert!(matches!(
        plan.acceptance,
        core_domain::Acceptance::Mechanical(_)
    ));
    let (mut record, mut blobs) = metadata_materialization(snapshot);
    record.files.remove("flange.brep");
    record.files.remove("flange.step");
    let aggregate_hash = replace_metadata_artifact(
        &mut record,
        &mut blobs,
        "model.brep",
        b"protocol-fixture-mechanical-aggregate".to_vec(),
    );
    let step_hash = replace_metadata_artifact(
        &mut record,
        &mut blobs,
        "model.step",
        b"protocol-fixture-mechanical-step".to_vec(),
    );
    for (node, hash) in &record.node_outputs {
        record.files.insert(
            format!("nodes/{node}.brep"),
            ArtifactRef {
                sha256: hash.clone(),
                size_bytes: blobs[hash].len() as u64,
            },
        );
    }
    let features: BTreeMap<_, _> = plan.features.iter().map(|f| (f.id, f)).collect();
    let mut instances = BTreeMap::new();
    let mut parts: BTreeMap<Uuid, serde_json::Value> = BTreeMap::new();
    for (node, feature) in &plan.outputs {
        let mut part = *feature;
        while features[&part].op == "shape.transform@1" {
            part = features[&part].inputs["shape"];
        }
        let item = &plan.scene[node];
        instances.insert(
            *node,
            json!({"name":item.name,"parent":item.parent,"feature_id":feature,"part_id":part}),
        );
        let definition = parts.entry(part).or_insert_with(
            || json!({"part_id":part,"name":features[&part].name,"quantity":0,"instances":[]}),
        );
        definition["quantity"] = json!(definition["quantity"].as_u64().unwrap() + 1);
        definition["instances"]
            .as_array_mut()
            .unwrap()
            .push(json!(node));
    }
    let assembly_hash = replace_metadata_artifact(
        &mut record,
        &mut blobs,
        "assembly.json",
        serde_json::to_vec(&json!({
            "schema_version":"deep3d.rigid-assembly/1",
            "project_id":plan.project_id,"revision_id":plan.revision_id,
            "scene":plan.scene,"instances":instances,"bom":parts.values().collect::<Vec<_>>()
        }))
        .unwrap(),
    );
    for (name, stage) in [("verify-brep.json", "native"), ("verify-step.json", "step")] {
        let report = json!({
            "schema_version":"deep3d.mechanical-verification/1", "status":"pass", "stage":stage,
            "expected_spec":plan.acceptance,"precision_profile":plan.precision,
            "input_artifact_sha256":if stage == "native" { &aggregate_hash } else { &step_hash },
            "input_node_artifact_sha256":if stage == "native" { json!(record.node_outputs) } else { json!({}) },
            "input_assembly_artifact_sha256":if stage == "native" { json!(assembly_hash) } else { json!(null) },
            "validator_id":"deep3d.mechanical@1",
            "validator_source_sha256":record.engine["verifier_source_sha256"],
            "checks":[{"id":"test.metadata_protocol_only","status":"pass"}],
        });
        replace_metadata_artifact(
            &mut record,
            &mut blobs,
            name,
            serde_json::to_vec(&report).unwrap(),
        );
    }
    let mut referenced: BTreeSet<_> = record
        .files
        .values()
        .map(|artifact| artifact.sha256.clone())
        .collect();
    referenced.insert(record.plan_hash.clone());
    blobs.retain(|hash, _| referenced.contains(hash));
    (record, blobs)
}

#[test]
fn changed_mechanical_instances_or_bom_cannot_reuse_original_verification() {
    for mutation in ["instances", "bom"] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("mechanical-protocol.deep3d");
        let project: Project = serde_json::from_str(include_str!(
            "../../../fixtures/mechanical/rigid-stack.json"
        ))
        .unwrap();
        let snapshot = ProjectStore::create(&path, &project).unwrap();
        let store = ProjectStore::open(path).unwrap();
        let (record, blobs) = mechanical_metadata_materialization(&snapshot);
        store
            .attach_materialization(record.clone(), blobs.clone())
            .unwrap();
        let before = fs::read(store.path()).unwrap();
        let mut changed_record = record.clone();
        let mut changed_blobs = blobs;
        let original_report = record.files["verify-brep.json"].clone();
        let mut assembly: serde_json::Value =
            serde_json::from_slice(&changed_blobs[&record.files["assembly.json"].sha256]).unwrap();
        if mutation == "instances" {
            assert_eq!(assembly["instances"].as_object().unwrap().len(), 6);
            assembly["instances"] = json!({});
        } else {
            assert_eq!(assembly["bom"].as_array().unwrap().len(), 2);
            assembly["bom"][0]["quantity"] = json!(999);
        }
        replace_metadata_artifact(
            &mut changed_record,
            &mut changed_blobs,
            "assembly.json",
            serde_json::to_vec(&assembly).unwrap(),
        );
        assert_eq!(changed_record.files["verify-brep.json"], original_report);
        assert_ne!(
            changed_record.files["assembly.json"],
            record.files["assembly.json"]
        );
        assert_eq!(
            store
                .attach_materialization(changed_record, changed_blobs)
                .unwrap_err()
                .code,
            "MATERIALIZATION_INVALID",
            "{mutation}"
        );
        assert_eq!(fs::read(store.path()).unwrap(), before, "{mutation}");
        let reopened = ProjectStore::open(store.path()).unwrap();
        let accepted = reopened.materializations(snapshot.revision.id).unwrap();
        assert_eq!(accepted.len(), 1, "{mutation}");
        assert_eq!(accepted[0].1, record, "{mutation}");
    }
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
