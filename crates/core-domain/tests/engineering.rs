use std::collections::BTreeSet;

use core_domain::*;
use serde_json::json;
use uuid::Uuid;

fn transaction(commands: Vec<Command>) -> Transaction {
    Transaction {
        id: Uuid::new_v4(),
        schema_version: TRANSACTION_SCHEMA.into(),
        base_revision: Uuid::new_v4(),
        expected_generation: 0,
        message: "Engineering edit".into(),
        commands,
    }
}

#[test]
fn compile_is_typed_stable_and_contains_real_feature_dependencies() {
    let project = Project::new_flange("Part").unwrap();
    let revision = Uuid::new_v4();
    let plan = project.compile(revision).unwrap();
    assert_eq!(plan.features.len(), 17);
    let mut seen = BTreeSet::new();
    for feature in &plan.features {
        assert!(feature.inputs.values().all(|input| seen.contains(input)));
        seen.insert(feature.id);
    }
    assert_eq!(
        plan.features
            .iter()
            .filter(|f| f.op == "profile.circle@1")
            .count(),
        6
    );
    assert_eq!(
        canonical_bytes(&plan).unwrap(),
        canonical_bytes(&project.compile(revision).unwrap()).unwrap()
    );
    assert_eq!(
        plan.acceptance.expected_volume_mm3(),
        26532.0 * std::f64::consts::PI
    );
}

#[test]
fn edits_preserve_ids_and_invalidate_only_the_dependent_closure() {
    let project = Project::new_flange("Part").unwrap();
    let parameter = project.parameter_id("thickness_mm").unwrap();
    let edited = apply_transaction(
        &project,
        &transaction(vec![Command::ParameterSet {
            id: parameter,
            value: ScalarValue::LengthMm(16.0),
        }]),
    )
    .unwrap();
    assert_eq!(project.project_id, edited.project_id);
    assert_eq!(
        project.features.keys().collect::<Vec<_>>(),
        edited.features.keys().collect::<Vec<_>>()
    );
    let affected = dirty_features(&project, &edited);
    assert_eq!(affected.len(), 11);
    assert!(edited
        .features
        .iter()
        .filter(|(_, f)| matches!(f.operation, Operation::Circle { .. }))
        .all(|(id, _)| !affected.contains(id)));
    assert_eq!(project.flange_spec().unwrap().thickness_mm, 12.0);
    assert_eq!(edited.flange_spec().unwrap().thickness_mm, 16.0);
}

#[test]
fn a_later_command_failure_cannot_mutate_the_original() {
    let project = Project::new_flange("Part").unwrap();
    let original = canonical_bytes(&project).unwrap();
    let edit = transaction(vec![
        Command::ParameterSet {
            id: project.parameter_id("thickness_mm").unwrap(),
            value: ScalarValue::LengthMm(16.0),
        },
        Command::ParameterSet {
            id: Uuid::new_v4(),
            value: ScalarValue::LengthMm(99.0),
        },
    ]);
    assert_eq!(
        apply_transaction(&project, &edit).unwrap_err().code,
        "PARAMETER_NOT_FOUND"
    );
    assert_eq!(canonical_bytes(&project).unwrap(), original);
}

#[test]
fn invalid_dimensions_and_unit_changes_are_rejected() {
    let project = Project::new_flange("Part").unwrap();
    let id = project.parameter_id("thickness_mm").unwrap();
    for value in [
        ScalarValue::LengthMm(-2.0),
        ScalarValue::LengthMm(f64::NAN),
        ScalarValue::AngleRad(12.0),
    ] {
        assert!(apply_transaction(
            &project,
            &transaction(vec![Command::ParameterSet { id, value }])
        )
        .is_err());
    }
    let pitch = project.parameter_id("bolt_circle_diameter_mm").unwrap();
    assert!(apply_transaction(
        &project,
        &transaction(vec![Command::ParameterSet {
            id: pitch,
            value: ScalarValue::LengthMm(99.0)
        }])
    )
    .is_err());
}

#[test]
fn rename_and_reparent_keep_identity_without_geometry_invalidation() {
    let project = Project::new_flange("Part").unwrap();
    let part = project
        .nodes
        .values()
        .find(|n| n.feature.is_some())
        .unwrap()
        .id;
    let group = Uuid::new_v4();
    let edited = apply_transaction(
        &project,
        &transaction(vec![
            Command::NodeCreate {
                node: SceneNode {
                    id: group,
                    name: "Assembly".into(),
                    parent: Some(project.root_node_id),
                    feature: None,
                },
            },
            Command::NodeReparent {
                id: part,
                parent: group,
            },
            Command::NodeRename {
                id: part,
                name: "Housing flange".into(),
            },
        ]),
    )
    .unwrap();
    assert_eq!(edited.nodes[&part].id, part);
    assert_eq!(edited.nodes[&part].parent, Some(group));
    assert!(dirty_features(&project, &edited).is_empty());
}

#[test]
fn scene_and_feature_cycles_and_dangling_references_fail() {
    let project = Project::new_flange("Part").unwrap();
    let part = project
        .nodes
        .values()
        .find(|n| n.feature.is_some())
        .unwrap()
        .id;
    assert_eq!(
        apply_transaction(
            &project,
            &transaction(vec![Command::NodeReparent {
                id: part,
                parent: part
            }])
        )
        .unwrap_err()
        .code,
        "HIERARCHY_CYCLE"
    );
    let mut cycle = project.clone();
    let id = cycle
        .features
        .values()
        .find(|f| matches!(f.operation, Operation::Cut { .. }))
        .unwrap()
        .id;
    let operation = &mut cycle.features.get_mut(&id).unwrap().operation;
    if let Operation::Cut { base, .. } = operation {
        *base = id;
    }
    assert_eq!(cycle.validate().unwrap_err().code, "DEPENDENCY_CYCLE");
    let profile = project
        .features
        .values()
        .find(|f| matches!(f.operation, Operation::Circle { .. }))
        .unwrap()
        .id;
    assert!(apply_transaction(
        &project,
        &transaction(vec![Command::FeatureDelete { id: profile }])
    )
    .is_err());
}

#[test]
fn geometry_port_types_and_cross_namespace_id_collision_are_checked() {
    let mut project = Project::new_flange("Part").unwrap();
    let circle = project
        .features
        .values()
        .find(|f| matches!(f.operation, Operation::Circle { .. }))
        .unwrap()
        .id;
    let cut = project
        .features
        .values()
        .find(|f| matches!(f.operation, Operation::Cut { .. }))
        .unwrap()
        .id;
    if let Operation::Cut { tool, .. } = &mut project.features.get_mut(&cut).unwrap().operation {
        *tool = circle;
    }
    assert_eq!(project.validate().unwrap_err().code, "TYPE_MISMATCH");
    let mut project = Project::new_flange("Part").unwrap();
    let id = *project.features.keys().next().unwrap();
    project.parameters.insert(
        id,
        Parameter {
            id,
            name: "collision".into(),
            value: ScalarValue::LengthMm(1.0),
        },
    );
    assert_eq!(project.validate().unwrap_err().code, "ID_COLLISION");
}

#[test]
fn same_value_and_empty_transactions_do_not_create_meaningless_versions() {
    let project = Project::new_flange("Part").unwrap();
    assert!(apply_transaction(&project, &transaction(vec![])).is_err());
    let id = project.parameter_id("thickness_mm").unwrap();
    assert_eq!(
        apply_transaction(
            &project,
            &transaction(vec![Command::ParameterSet {
                id,
                value: ScalarValue::LengthMm(12.0)
            }])
        )
        .unwrap_err()
        .code,
        "EMPTY_CHANGE"
    );
}

#[test]
fn canonical_content_and_decimal_generation_have_portable_identity() {
    assert_eq!(
        canonical_bytes(&json!({"z":1.0,"a":-0.0})).unwrap(),
        br#"{"a":0,"z":1}"#
    );
    let mut tx = transaction(vec![]);
    tx.expected_generation = u64::MAX;
    let encoded = serde_json::to_value(&tx).unwrap();
    assert_eq!(encoded["expected_generation"], u64::MAX.to_string());
    let mut invalid = encoded;
    invalid["expected_generation"] = json!(1);
    assert!(serde_json::from_value::<Transaction>(invalid).is_err());
    let mut invalid = serde_json::to_value(project()).unwrap();
    invalid["unexpected"] = json!(true);
    assert!(serde_json::from_value::<Project>(invalid).is_err());
}

fn project() -> Project {
    Project::new_flange("Fixture").unwrap()
}

#[test]
fn precision_changes_dirty_all_outputs_and_snapshot_roundtrip_preserves_identity() {
    let old = project();
    let mut new = old.clone();
    new.precision.working_tolerance_mm = 2e-5;
    assert_eq!(dirty_features(&old, &new).len(), old.features.len());
    let decoded: Project = serde_json::from_slice(&canonical_bytes(&old).unwrap()).unwrap();
    assert_eq!(decoded, old);
}
