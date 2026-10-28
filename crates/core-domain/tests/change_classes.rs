use core_domain::*;
use uuid::Uuid;

fn rigid_stack() -> Project {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../fixtures/mechanical/rigid-stack.json"
    );
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

fn feature(project: &Project, name: &str) -> Uuid {
    project
        .features
        .values()
        .find(|f| f.name == name)
        .unwrap()
        .id
}

fn set(project: &mut Project, name: &str, value: f64) {
    let id = project.parameter_id(name).unwrap();
    project.parameters.get_mut(&id).unwrap().value = ScalarValue::LengthMm(value);
}

#[test]
fn moving_unchanged_shapes_is_placement_not_geometry() {
    let old = rigid_stack();
    let mut new = old.clone();
    set(&mut new, "plate_thickness_mm", 6.0);
    let plan = invalidation_plan(&old, &new);
    let classes = classify(&old, &new, &plan);
    let plate = feature(&old, "PlateBody");
    let spacer = feature(&old, "SpacerBody");
    assert!(classes.geometry.contains(&plate));
    assert!(
        !plan.dirty.contains(&spacer),
        "spacer definition does not read plate thickness"
    );
    for i in 1..=4 {
        // Spacers rise with the plate but their shared shape is reused.
        let placement = feature(&old, &format!("SpacerPlacement{i}"));
        assert!(classes.placement.contains(&placement));
        assert!(!classes.geometry.contains(&placement));
    }
    // The top plate's transform moves a changed shape, so it is geometry.
    assert!(classes
        .geometry
        .contains(&feature(&old, "TopPlatePlacement")));
    assert!(classes.delivery.is_empty());
}

#[test]
fn renaming_a_node_is_delivery_only() {
    let old = rigid_stack();
    let mut new = old.clone();
    let node = *new
        .nodes
        .keys()
        .find(|id| new.nodes[*id].feature.is_some())
        .unwrap();
    new.nodes.get_mut(&node).unwrap().name = "renamed".into();
    let plan = invalidation_plan(&old, &new);
    let classes = classify(&old, &new, &plan);
    assert!(plan.dirty.is_empty());
    assert!(classes.geometry.is_empty() && classes.placement.is_empty());
    assert_eq!(classes.delivery, [node].into());
    assert_eq!(classes.evidence, [node].into());
}

#[test]
fn linear_expressions_sum_parameters_and_track_every_term() {
    let mut new = rigid_stack();
    let top = feature(&new, "TopPlatePlacement");
    let terms = ["plate_thickness_mm", "spacer_height_mm"]
        .map(|name| LinearTerm {
            parameter_id: new.parameter_id(name).unwrap(),
            scale: 1.0,
        })
        .to_vec();
    let Operation::Transform { translation, .. } =
        &mut new.features.get_mut(&top).unwrap().operation
    else {
        panic!("top plate placement is a transform");
    };
    translation[2] = ScalarExpr::Linear {
        terms,
        offset: ScalarValue::LengthMm(0.0),
    };
    new.validate().unwrap();
    let plan = new.compile(Uuid::new_v4()).unwrap();
    let placed = plan.features.iter().find(|f| f.id == top).unwrap();
    assert_eq!(placed.parameters["dz_mm"], 15.0);
    assert!(new.features[&top].operation.parameter_ids().len() == 2);

    let mut edited = new.clone();
    set(&mut edited, "spacer_height_mm", 12.0);
    assert!(invalidation_plan(&new, &edited).dirty.contains(&top));

    let mut wrong = new.clone();
    let Operation::Transform { translation, .. } =
        &mut wrong.features.get_mut(&top).unwrap().operation
    else {
        unreachable!()
    };
    let ScalarExpr::Linear { offset, .. } = &mut translation[2] else {
        unreachable!()
    };
    *offset = ScalarValue::AngleRad(0.0);
    assert_eq!(wrong.validate().unwrap_err().code, "TYPE_MISMATCH");
}

fn holes(diameter: f64, wall: Option<f64>) -> NodeAcceptance {
    NodeAcceptance {
        solid_count: 1,
        volume_mm3: None,
        bounds_mm: Some([-40.0, -25.0, 0.0, 40.0, 25.0, 8.0]),
        axial_holes: Some(AxialHoles {
            axis: Axis::Z,
            diameter_mm: diameter,
            centers_mm: vec![[-25.0, -15.0], [-25.0, 15.0], [25.0, -15.0], [25.0, 15.0]],
            through: true,
            min_wall_mm: wall,
        }),
    }
}

#[test]
fn consistent_hole_requirements_are_accepted() {
    holes(6.0, Some(2.0)).validate().unwrap();
    // Centers sit 10 mm from the long edges: 16 mm holes leave exactly 2 mm.
    holes(16.0, Some(2.0)).validate().unwrap();
    let error = holes(17.0, Some(2.0)).validate().unwrap_err();
    assert!(error.message.contains("minimum_wall_violation[0,outer]"));
}

#[test]
fn contradictory_hole_requirements_name_their_geometric_witnesses() {
    let error = holes(60.0, Some(2.0)).validate().unwrap_err();
    assert_eq!(error.code, "GOAL_CONFLICT");
    for witness in [
        "hole_overlap",
        "hole_outside_stock",
        "minimum_wall_violation",
    ] {
        assert!(
            error.message.contains(witness),
            "{witness}: {}",
            error.message
        );
    }
    let error = holes(29.0, Some(0.0)).validate().unwrap_err();
    assert!(error.message.contains("hole_outside_stock") && !error.message.contains("overlap"));
    let mut sideways = holes(6.0, None);
    sideways.axial_holes.as_mut().unwrap().axis = Axis::X;
    assert_eq!(sideways.validate().unwrap_err().code, "ACCEPTANCE_INVALID");
}
