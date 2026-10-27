use core_domain::*;
use serde_json::json;
use std::collections::BTreeMap;
use uuid::Uuid;

fn l(value: f64) -> ScalarExpr {
    ScalarExpr::length_mm(value)
}
fn a(value: f64) -> ScalarExpr {
    ScalarExpr::angle_rad(value)
}
fn origin() -> [ScalarExpr; 3] {
    [l(0.0), l(0.0), l(0.0)]
}

fn cuboid() -> Project {
    let width = Uuid::new_v4();
    let profile = Uuid::new_v4();
    let solid = Uuid::new_v4();
    let root = Uuid::new_v4();
    let part = Uuid::new_v4();
    Project::new_mechanical(
        "Mechanical part",
        BTreeMap::from([(
            width,
            Parameter {
                id: width,
                name: "width_mm".into(),
                value: ScalarValue::LengthMm(2.0),
            },
        )]),
        BTreeMap::from([
            (
                profile,
                Feature {
                    id: profile,
                    name: "rectangle".into(),
                    operation: Operation::Rectangle {
                        width: ScalarExpr::parameter(width, 1.0),
                        height: l(3.0),
                        center_x: l(0.0),
                        center_y: l(0.0),
                    },
                },
            ),
            (
                solid,
                Feature {
                    id: solid,
                    name: "cuboid".into(),
                    operation: Operation::Extrude {
                        profile,
                        distance: l(4.0),
                    },
                },
            ),
        ]),
        BTreeMap::from([
            (
                root,
                SceneNode {
                    id: root,
                    name: "World".into(),
                    parent: None,
                    feature: None,
                },
            ),
            (
                part,
                SceneNode {
                    id: part,
                    name: "Part".into(),
                    parent: Some(root),
                    feature: Some(solid),
                },
            ),
        ]),
        root,
        GeneralAcceptance::new(BTreeMap::from([(
            part,
            NodeAcceptance {
                solid_count: 1,
                volume_mm3: Some(24.0),
                bounds_mm: Some([-1.0, -1.5, 0.0, 1.0, 1.5, 4.0]),
            },
        )])),
    )
    .unwrap()
}

fn add(project: &mut Project, name: &str, operation: Operation) -> Uuid {
    let id = Uuid::new_v4();
    project.features.insert(
        id,
        Feature {
            id,
            name: name.into(),
            operation,
        },
    );
    id
}

fn ids(project: &Project) -> (Uuid, Uuid) {
    let profile = project
        .features
        .values()
        .find(|f| matches!(f.operation, Operation::Rectangle { .. }))
        .unwrap()
        .id;
    let solid = project.nodes.values().find_map(|n| n.feature).unwrap();
    (profile, solid)
}

#[test]
fn legacy_flange_wire_shape_and_old_plan_fields_remain_unchanged() {
    let project = Project::new_flange("Legacy").unwrap();
    let raw = serde_json::to_value(&project).unwrap();
    assert!(raw["flange"].is_object());
    assert!(raw.get("acceptance").is_none());
    let restored: Project = serde_json::from_value(raw).unwrap();
    assert_eq!(project, restored);
    let plan = serde_json::to_value(project.compile(Uuid::new_v4()).unwrap()).unwrap();
    assert!(plan.get("scene").is_none());
    assert_eq!(plan["acceptance"]["schema_version"], "deep3d.flange/1");
    assert!(plan["features"]
        .as_array()
        .unwrap()
        .iter()
        .all(|f| f.get("data").is_none()));
}

#[test]
fn generalized_project_requires_external_targets_and_covers_every_output_leaf() {
    let project = cuboid();
    assert!(project.flange.is_none());
    let plan = project.compile(Uuid::new_v4()).unwrap();
    assert_eq!(plan.scene, project.nodes);
    assert!(matches!(plan.acceptance, Acceptance::Mechanical(_)));
    assert_eq!(
        serde_json::to_value(&plan).unwrap()["features"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    let mut invalid = project.clone();
    invalid.acceptance.as_mut().unwrap().nodes.clear();
    assert_eq!(invalid.validate().unwrap_err().code, "ACCEPTANCE_INVALID");
    let mut invalid = project.clone();
    let acceptance = invalid
        .acceptance
        .as_mut()
        .unwrap()
        .nodes
        .values_mut()
        .next()
        .unwrap();
    acceptance.volume_mm3 = None;
    acceptance.bounds_mm = None;
    assert_eq!(invalid.validate().unwrap_err().code, "ACCEPTANCE_INVALID");
    let mut invalid = project.clone();
    let part = invalid
        .nodes
        .values()
        .find(|node| node.feature.is_some())
        .unwrap()
        .id;
    let child = Uuid::new_v4();
    invalid.nodes.insert(
        child,
        SceneNode {
            id: child,
            name: "Invalid child".into(),
            parent: Some(part),
            feature: None,
        },
    );
    assert_eq!(invalid.validate().unwrap_err().code, "HIERARCHY_INVALID");
}

#[test]
fn all_mechanical_operations_compile_to_explicit_typed_ports_and_resolved_data() {
    let mut project = cuboid();
    let (profile, solid) = ids(&project);
    let transformed = add(
        &mut project,
        "placement",
        Operation::Transform {
            shape: solid,
            translation: [l(0.5), l(0.0), l(0.0)],
            axis: [0.0, 0.0, 1.0],
            angle: a(0.0),
        },
    );
    add(
        &mut project,
        "polygon",
        Operation::Polygon {
            points: vec![[l(0.0), l(0.0)], [l(2.0), l(0.0)], [l(0.0), l(2.0)]],
        },
    );
    add(
        &mut project,
        "revolve",
        Operation::Revolve {
            profile,
            angle: a(std::f64::consts::TAU),
            axis_origin: origin(),
            axis_direction: [0.0, 1.0, 0.0],
        },
    );
    add(
        &mut project,
        "fuse",
        Operation::Fuse {
            base: solid,
            tool: transformed,
        },
    );
    add(
        &mut project,
        "common",
        Operation::Common {
            base: solid,
            tool: transformed,
        },
    );
    add(
        &mut project,
        "cut",
        Operation::Cut {
            base: solid,
            tool: transformed,
        },
    );
    add(
        &mut project,
        "fillet",
        Operation::Fillet {
            shape: solid,
            radius: l(0.1),
            selector: EdgeSelector::LineParallel {
                axis: [0.0, 0.0, 1.0],
                expected_count: 4,
            },
        },
    );
    add(
        &mut project,
        "chamfer",
        Operation::Chamfer {
            shape: solid,
            distance: l(0.1),
            selector: EdgeSelector::Plane {
                axis: Axis::Z,
                coordinate: l(4.0),
                expected_count: 4,
            },
        },
    );
    add(
        &mut project,
        "linear",
        Operation::LinearPattern {
            shape: solid,
            count: 2,
            step: [l(3.0), l(0.0), l(0.0)],
            fuse: false,
        },
    );
    add(
        &mut project,
        "circular",
        Operation::CircularPattern {
            shape: transformed,
            count: 3,
            axis: [0.0, 0.0, 1.0],
            origin: origin(),
            step_angle: a(std::f64::consts::TAU / 3.0),
            fuse: false,
        },
    );
    add(
        &mut project,
        "hole",
        Operation::Hole {
            base: solid,
            diameter: l(0.5),
            depth: l(4.0),
            centers: vec![[l(0.0), l(0.0)]],
            start_z: l(0.0),
        },
    );
    add(
        &mut project,
        "instances",
        Operation::Compound {
            parts: vec![solid, solid],
        },
    );
    let revision = Uuid::new_v4();
    let plan = project.compile(revision).unwrap();
    let repeated = project.compile(revision).unwrap();
    assert_eq!(
        canonical_bytes(&plan).unwrap(),
        canonical_bytes(&repeated).unwrap()
    );
    let by_name: BTreeMap<_, _> = plan.features.iter().map(|f| (f.name.as_str(), f)).collect();
    assert_eq!(by_name["placement"].parameters["dx_mm"], 0.5);
    assert_eq!(by_name["fillet"].data["selector"]["kind"], "line_parallel");
    assert_eq!(
        by_name["circular"].parameters["step_angle_rad"],
        std::f64::consts::TAU / 3.0
    );
    assert_eq!(by_name["linear"].data["count"], 2);
    assert_eq!(by_name["hole"].data["centers_mm"], json!([[0.0, 0.0]]));
    assert_eq!(by_name["instances"].inputs.len(), 2);
    assert_eq!(
        by_name["instances"].inputs["part_0"],
        by_name["instances"].inputs["part_1"]
    );
}

#[test]
fn type_selector_polygon_and_exponential_pattern_errors_are_rejected_before_execution() {
    let project = cuboid();
    let (profile, solid) = ids(&project);
    let invalid = [
        Operation::Revolve {
            profile,
            angle: l(1.0),
            axis_origin: origin(),
            axis_direction: [0.0, 1.0, 0.0],
        },
        Operation::Transform {
            shape: solid,
            translation: origin(),
            axis: [0.0, 0.0, 2.0],
            angle: a(0.0),
        },
        Operation::Fillet {
            shape: profile,
            radius: l(0.1),
            selector: EdgeSelector::AllEdges { expected_count: 4 },
        },
        Operation::Chamfer {
            shape: solid,
            distance: l(0.1),
            selector: EdgeSelector::AllEdges { expected_count: 0 },
        },
        Operation::Polygon {
            points: vec![
                [l(0.0), l(0.0)],
                [l(1.0), l(1.0)],
                [l(0.0), l(1.0)],
                [l(1.0), l(0.0)],
            ],
        },
        Operation::CircularPattern {
            shape: solid,
            count: 4,
            axis: [0.0, 0.0, 1.0],
            origin: origin(),
            step_angle: a(std::f64::consts::PI),
            fuse: false,
        },
    ];
    for operation in invalid {
        let mut invalid = project.clone();
        add(&mut invalid, "Invalid", operation);
        assert!(invalid.validate().is_err());
    }
    let mut explosive = project.clone();
    let first = add(
        &mut explosive,
        "pattern",
        Operation::LinearPattern {
            shape: solid,
            count: 128,
            step: [l(3.0), l(0.0), l(0.0)],
            fuse: false,
        },
    );
    add(
        &mut explosive,
        "nested",
        Operation::LinearPattern {
            shape: first,
            count: 128,
            step: [l(0.0), l(3.0), l(0.0)],
            fuse: false,
        },
    );
    assert_eq!(explosive.validate().unwrap_err().code, "PROJECT_LIMIT");
}

fn rectangle_sketch(width: Uuid) -> SketchDefinition {
    SketchDefinition {
        points: BTreeMap::from([
            ("p0".into(), [l(0.0), l(0.0)]),
            ("p1".into(), [l(2.0), l(0.0)]),
            ("p2".into(), [l(2.0), l(3.0)]),
            ("p3".into(), [l(0.0), l(3.0)]),
        ]),
        lines: BTreeMap::from([
            ("e0".into(), ["p0".into(), "p1".into()]),
            ("e1".into(), ["p1".into(), "p2".into()]),
            ("e2".into(), ["p2".into(), "p3".into()]),
            ("e3".into(), ["p3".into(), "p0".into()]),
        ]),
        circles: BTreeMap::new(),
        arcs: BTreeMap::new(),
        constraints: vec![
            SketchConstraint::Fix {
                id: "origin".into(),
                point: "p0".into(),
                x: l(0.0),
                y: l(0.0),
            },
            SketchConstraint::Horizontal {
                id: "h0".into(),
                line: "e0".into(),
            },
            SketchConstraint::Vertical {
                id: "v1".into(),
                line: "e1".into(),
            },
            SketchConstraint::Horizontal {
                id: "h2".into(),
                line: "e2".into(),
            },
            SketchConstraint::Vertical {
                id: "v3".into(),
                line: "e3".into(),
            },
            SketchConstraint::Distance {
                id: "width".into(),
                a: "p0".into(),
                b: "p1".into(),
                value: ScalarExpr::parameter(width, 1.0),
            },
            SketchConstraint::Distance {
                id: "height".into(),
                a: "p1".into(),
                b: "p2".into(),
                value: l(3.0),
            },
        ],
        loops: vec![(0..4)
            .map(|i| SketchEdgeRef {
                entity: format!("e{i}"),
                reversed: false,
            })
            .collect()],
    }
}

#[test]
fn dimensional_sketch_and_acceptance_edit_together_without_replacing_stable_ids() {
    let mut project = cuboid();
    let (profile, _) = ids(&project);
    let width = project.parameter_id("width_mm").unwrap();
    project.features.get_mut(&profile).unwrap().operation = Operation::Sketch {
        sketch: rectangle_sketch(width),
    };
    project
        .acceptance
        .as_mut()
        .unwrap()
        .nodes
        .values_mut()
        .next()
        .unwrap()
        .bounds_mm = Some([0.0, 0.0, 0.0, 2.0, 3.0, 4.0]);
    let mut target = project.acceptance.clone().unwrap();
    let expected = target.nodes.values_mut().next().unwrap();
    expected.volume_mm3 = Some(48.0);
    expected.bounds_mm = Some([0.0, 0.0, 0.0, 4.0, 3.0, 4.0]);
    let changed = apply_transaction(
        &project,
        &Transaction {
            id: Uuid::new_v4(),
            schema_version: TRANSACTION_SCHEMA.into(),
            base_revision: Uuid::new_v4(),
            expected_generation: 0,
            message: "Change mechanical dimension and agreed target".into(),
            commands: vec![
                Command::ParameterSet {
                    id: width,
                    value: ScalarValue::LengthMm(4.0),
                },
                Command::AcceptanceSet { acceptance: target },
            ],
        },
    )
    .unwrap();
    assert_eq!(
        changed.features.keys().collect::<Vec<_>>(),
        project.features.keys().collect::<Vec<_>>()
    );
    assert_eq!(dirty_features(&project, &changed).len(), 2);
    let plan = changed.compile(Uuid::new_v4()).unwrap();
    let sketch = &plan.features.iter().find(|f| f.id == profile).unwrap().data["sketch"];
    assert_eq!(sketch["constraints"][5]["value_mm"], 4.0);
    assert_eq!(sketch["points"]["p1"][0], 2.0); // Seeds are not the driving constraint.
    let mut bad = changed.clone();
    if let Operation::Sketch { sketch } = &mut bad.features.get_mut(&profile).unwrap().operation {
        sketch.constraints.push(SketchConstraint::Horizontal {
            id: "missing".into(),
            line: "unknown".into(),
        });
    }
    assert_eq!(bad.validate().unwrap_err().code, "SKETCH_INVALID");
}
