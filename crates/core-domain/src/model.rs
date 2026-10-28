use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::{CoreError, Result, SketchDefinition};

pub const SPEC_SCHEMA: &str = "deep3d.spec/1";
pub const TRANSACTION_SCHEMA: &str = "deep3d.transaction/1";
pub const PLAN_SCHEMA: &str = "deep3d.execution-plan/1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScalarUnit {
    LengthMm,
    AngleRad,
    Unitless,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum ScalarValue {
    LengthMm(f64),
    AngleRad(f64),
    Unitless(f64),
}

impl ScalarValue {
    pub fn unit(&self) -> ScalarUnit {
        match self {
            Self::LengthMm(_) => ScalarUnit::LengthMm,
            Self::AngleRad(_) => ScalarUnit::AngleRad,
            Self::Unitless(_) => ScalarUnit::Unitless,
        }
    }

    pub fn number(&self) -> f64 {
        match self {
            Self::LengthMm(value) | Self::AngleRad(value) | Self::Unitless(value) => *value,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ScalarExpr {
    Literal {
        value: ScalarValue,
    },
    Parameter {
        parameter_id: Uuid,
        scale: f64,
    },
    /// `offset + sum(scale * parameter)`; every term shares the offset's unit.
    Linear {
        terms: Vec<LinearTerm>,
        offset: ScalarValue,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LinearTerm {
    pub parameter_id: Uuid,
    pub scale: f64,
}

impl ScalarExpr {
    pub fn length_mm(value: f64) -> Self {
        Self::Literal {
            value: ScalarValue::LengthMm(value),
        }
    }

    pub fn parameter(parameter_id: Uuid, scale: f64) -> Self {
        Self::Parameter {
            parameter_id,
            scale,
        }
    }

    pub fn angle_rad(value: f64) -> Self {
        Self::Literal {
            value: ScalarValue::AngleRad(value),
        }
    }

    pub(crate) fn parameter_ids(&self) -> BTreeSet<Uuid> {
        match self {
            Self::Literal { .. } => BTreeSet::new(),
            Self::Parameter { parameter_id, .. } => BTreeSet::from([*parameter_id]),
            Self::Linear { terms, .. } => terms.iter().map(|term| term.parameter_id).collect(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Parameter {
    pub id: Uuid,
    pub name: String,
    pub value: ScalarValue,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SceneNode {
    pub id: Uuid,
    pub name: String,
    pub parent: Option<Uuid>,
    pub feature: Option<Uuid>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
pub enum Operation {
    #[serde(rename = "profile.circle@1")]
    Circle {
        radius: ScalarExpr,
        center_x: ScalarExpr,
        center_y: ScalarExpr,
    },
    #[serde(rename = "profile.rectangle@1")]
    Rectangle {
        width: ScalarExpr,
        height: ScalarExpr,
        center_x: ScalarExpr,
        center_y: ScalarExpr,
    },
    #[serde(rename = "profile.polygon@1")]
    Polygon { points: Vec<[ScalarExpr; 2]> },
    #[serde(rename = "profile.sketch@1")]
    Sketch { sketch: SketchDefinition },
    #[serde(rename = "solid.extrude@1")]
    Extrude { profile: Uuid, distance: ScalarExpr },
    #[serde(rename = "solid.cut@1")]
    Cut { base: Uuid, tool: Uuid },
    #[serde(rename = "solid.fuse@1")]
    Fuse { base: Uuid, tool: Uuid },
    #[serde(rename = "solid.common@1")]
    Common { base: Uuid, tool: Uuid },
    #[serde(rename = "solid.revolve@1")]
    Revolve {
        profile: Uuid,
        angle: ScalarExpr,
        axis_origin: [ScalarExpr; 3],
        axis_direction: [f64; 3],
    },
    #[serde(rename = "shape.transform@1")]
    Transform {
        shape: Uuid,
        translation: [ScalarExpr; 3],
        axis: [f64; 3],
        angle: ScalarExpr,
    },
    #[serde(rename = "solid.fillet@1")]
    Fillet {
        shape: Uuid,
        radius: ScalarExpr,
        selector: EdgeSelector,
    },
    #[serde(rename = "solid.chamfer@1")]
    Chamfer {
        shape: Uuid,
        distance: ScalarExpr,
        selector: EdgeSelector,
    },
    #[serde(rename = "shape.linear_pattern@1")]
    LinearPattern {
        shape: Uuid,
        count: u32,
        step: [ScalarExpr; 3],
        fuse: bool,
    },
    #[serde(rename = "shape.circular_pattern@1")]
    CircularPattern {
        shape: Uuid,
        count: u32,
        axis: [f64; 3],
        origin: [ScalarExpr; 3],
        step_angle: ScalarExpr,
        fuse: bool,
    },
    #[serde(rename = "solid.hole@1")]
    Hole {
        base: Uuid,
        diameter: ScalarExpr,
        depth: ScalarExpr,
        centers: Vec<[ScalarExpr; 2]>,
        start_z: ScalarExpr,
    },
    #[serde(rename = "assembly.compound@1")]
    Compound { parts: Vec<Uuid> },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Axis {
    X,
    Y,
    Z,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum EdgeSelector {
    AllEdges {
        expected_count: u32,
    },
    LineParallel {
        axis: [f64; 3],
        expected_count: u32,
    },
    Plane {
        axis: Axis,
        coordinate: ScalarExpr,
        expected_count: u32,
    },
    Circle {
        radius: ScalarExpr,
        axis: Axis,
        coordinate: ScalarExpr,
        expected_count: u32,
    },
    /// Straight edges parallel to `direction` that lie in the plane `axis = coordinate`.
    LineParallelInPlane {
        direction: [f64; 3],
        axis: Axis,
        coordinate: ScalarExpr,
        expected_count: u32,
    },
}

impl EdgeSelector {
    pub fn expected_count(&self) -> u32 {
        match self {
            Self::AllEdges { expected_count }
            | Self::LineParallel { expected_count, .. }
            | Self::Plane { expected_count, .. }
            | Self::Circle { expected_count, .. }
            | Self::LineParallelInPlane { expected_count, .. } => *expected_count,
        }
    }

    pub(crate) fn expressions(&self) -> Vec<&ScalarExpr> {
        match self {
            Self::Plane { coordinate, .. } | Self::LineParallelInPlane { coordinate, .. } => {
                vec![coordinate]
            }
            Self::Circle {
                radius, coordinate, ..
            } => vec![radius, coordinate],
            _ => vec![],
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputType {
    PlanarProfile,
    /// One or more closed solids; exact multiplicity belongs to acceptance checks.
    BRepSolid,
}

impl Operation {
    pub fn output_type(&self) -> OutputType {
        match self {
            Self::Circle { .. }
            | Self::Rectangle { .. }
            | Self::Polygon { .. }
            | Self::Sketch { .. } => OutputType::PlanarProfile,
            _ => OutputType::BRepSolid,
        }
    }

    pub fn dependencies(&self) -> BTreeSet<Uuid> {
        match self {
            Self::Circle { .. }
            | Self::Rectangle { .. }
            | Self::Polygon { .. }
            | Self::Sketch { .. } => BTreeSet::new(),
            Self::Extrude { profile, .. } | Self::Revolve { profile, .. } => {
                BTreeSet::from([*profile])
            }
            Self::Cut { base, tool } | Self::Fuse { base, tool } | Self::Common { base, tool } => {
                BTreeSet::from([*base, *tool])
            }
            Self::Transform { shape, .. }
            | Self::Fillet { shape, .. }
            | Self::Chamfer { shape, .. }
            | Self::LinearPattern { shape, .. }
            | Self::CircularPattern { shape, .. } => BTreeSet::from([*shape]),
            Self::Hole { base, .. } => BTreeSet::from([*base]),
            Self::Compound { parts } => parts.iter().copied().collect(),
        }
    }

    pub fn parameter_ids(&self) -> BTreeSet<Uuid> {
        self.expressions()
            .into_iter()
            .flat_map(ScalarExpr::parameter_ids)
            .collect()
    }

    pub(crate) fn expressions(&self) -> Vec<&ScalarExpr> {
        match self {
            Self::Circle {
                radius,
                center_x,
                center_y,
            } => vec![radius, center_x, center_y],
            Self::Rectangle {
                width,
                height,
                center_x,
                center_y,
            } => vec![width, height, center_x, center_y],
            Self::Polygon { points } => points.iter().flat_map(|point| point.iter()).collect(),
            Self::Sketch { sketch } => sketch.expressions(),
            Self::Extrude { distance, .. } => vec![distance],
            Self::Revolve {
                angle, axis_origin, ..
            } => axis_origin.iter().chain([angle]).collect(),
            Self::Transform {
                translation, angle, ..
            } => translation.iter().chain([angle]).collect(),
            Self::Fillet {
                radius, selector, ..
            } => [radius].into_iter().chain(selector.expressions()).collect(),
            Self::Chamfer {
                distance, selector, ..
            } => [distance]
                .into_iter()
                .chain(selector.expressions())
                .collect(),
            Self::LinearPattern { step, .. } => step.iter().collect(),
            Self::CircularPattern {
                origin, step_angle, ..
            } => origin.iter().chain([step_angle]).collect(),
            Self::Hole {
                diameter,
                depth,
                centers,
                start_z,
                ..
            } => [diameter, depth, start_z]
                .into_iter()
                .chain(centers.iter().flat_map(|point| point.iter()))
                .collect(),
            Self::Cut { .. } | Self::Fuse { .. } | Self::Common { .. } | Self::Compound { .. } => {
                vec![]
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Feature {
    pub id: Uuid,
    pub name: String,
    pub operation: Operation,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlangeBinding {
    pub outer_diameter: Uuid,
    pub thickness: Uuid,
    pub center_hole_diameter: Uuid,
    pub bolt_hole_diameter: Uuid,
    pub bolt_circle_diameter: Uuid,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlangeSpec {
    pub schema_version: String,
    pub units: String,
    pub coordinate_system: String,
    pub outer_diameter_mm: f64,
    pub thickness_mm: f64,
    pub center_hole_diameter_mm: f64,
    pub bolt_hole_diameter_mm: f64,
    pub bolt_circle_diameter_mm: f64,
    pub hole_count: u32,
    pub start_angle_rad: f64,
}

impl Default for FlangeSpec {
    fn default() -> Self {
        Self {
            schema_version: "deep3d.flange/1".into(),
            units: "mm".into(),
            coordinate_system: "right_handed_z_up".into(),
            outer_diameter_mm: 100.0,
            thickness_mm: 12.0,
            center_hole_diameter_mm: 30.0,
            bolt_hole_diameter_mm: 8.0,
            bolt_circle_diameter_mm: 70.0,
            hole_count: 4,
            start_angle_rad: 0.0,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrecisionProfile {
    pub schema_version: String,
    pub working_tolerance_mm: f64,
    pub max_subshape_tolerance_mm: f64,
    pub critical_dimension_error_mm: f64,
    pub step_roundtrip_dimension_error_mm: f64,
    pub volume_relative_error: f64,
    pub measurement_error_budget_mm: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NodeAcceptance {
    pub solid_count: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub volume_mm3: Option<f64>,
    /// [xmin, ymin, zmin, xmax, ymax, zmax], in global mm coordinates.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bounds_mm: Option<[f64; 6]>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GeneralAcceptance {
    pub schema_version: String,
    pub nodes: BTreeMap<Uuid, NodeAcceptance>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assembly: Option<NodeAcceptance>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_interference_volume_mm3: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Acceptance {
    Flange(FlangeSpec),
    Mechanical(GeneralAcceptance),
}

impl Acceptance {
    pub fn as_flange(&self) -> Result<&FlangeSpec> {
        match self {
            Self::Flange(value) => Ok(value),
            _ => Err(CoreError::new(
                "TYPE_MISMATCH",
                "Acceptance is not a flange contract",
            )),
        }
    }
}

impl Default for PrecisionProfile {
    fn default() -> Self {
        Self {
            schema_version: "deep3d.precision/1".into(),
            working_tolerance_mm: 0.00001,
            max_subshape_tolerance_mm: 0.001,
            critical_dimension_error_mm: 0.001,
            step_roundtrip_dimension_error_mm: 0.005,
            volume_relative_error: 0.000001,
            measurement_error_budget_mm: 0.0001,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Project {
    pub schema_version: String,
    pub project_id: Uuid,
    pub name: String,
    pub root_node_id: Uuid,
    pub nodes: BTreeMap<Uuid, SceneNode>,
    pub parameters: BTreeMap<Uuid, Parameter>,
    pub features: BTreeMap<Uuid, Feature>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flange: Option<FlangeBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acceptance: Option<GeneralAcceptance>,
    pub precision: PrecisionProfile,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlannedFeature {
    pub id: Uuid,
    pub name: String,
    pub op: String,
    pub inputs: BTreeMap<String, Uuid>,
    pub parameters: BTreeMap<String, f64>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub data: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionPlan {
    pub schema_version: String,
    pub project_id: Uuid,
    pub revision_id: Uuid,
    pub units: String,
    pub coordinate_system: String,
    pub features: Vec<PlannedFeature>,
    pub outputs: BTreeMap<Uuid, Uuid>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub scene: BTreeMap<Uuid, SceneNode>,
    pub acceptance: Acceptance,
    pub precision: PrecisionProfile,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Transaction {
    pub id: Uuid,
    pub schema_version: String,
    pub base_revision: Uuid,
    #[serde(with = "crate::u64_string")]
    pub expected_generation: u64,
    pub message: String,
    pub commands: Vec<Command>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
pub enum Command {
    #[serde(rename = "acceptance.set")]
    AcceptanceSet { acceptance: GeneralAcceptance },
    #[serde(rename = "parameter.set")]
    ParameterSet { id: Uuid, value: ScalarValue },
    #[serde(rename = "parameter.create")]
    ParameterCreate { parameter: Parameter },
    #[serde(rename = "node.rename")]
    NodeRename { id: Uuid, name: String },
    #[serde(rename = "node.reparent")]
    NodeReparent { id: Uuid, parent: Uuid },
    #[serde(rename = "feature.create")]
    FeatureCreate { feature: Feature },
    #[serde(rename = "feature.replace")]
    FeatureReplace { feature: Feature },
    #[serde(rename = "feature.delete")]
    FeatureDelete { id: Uuid },
    #[serde(rename = "node.create")]
    NodeCreate { node: SceneNode },
    #[serde(rename = "node.delete")]
    NodeDelete { id: Uuid },
}

impl Project {
    pub fn parameter_id(&self, name: &str) -> Result<Uuid> {
        self.parameters
            .values()
            .find(|parameter| parameter.name == name)
            .map(|parameter| parameter.id)
            .ok_or_else(|| {
                CoreError::new("PARAMETER_NOT_FOUND", format!("Unknown parameter {name}"))
            })
    }

    pub fn new_flange(name: &str) -> Result<Self> {
        let mut parameters = BTreeMap::new();
        let mut parameter = |name: &str, number: f64| {
            let id = Uuid::new_v4();
            parameters.insert(
                id,
                Parameter {
                    id,
                    name: name.into(),
                    value: ScalarValue::LengthMm(number),
                },
            );
            id
        };
        let flange = FlangeBinding {
            outer_diameter: parameter("outer_diameter_mm", 100.0),
            thickness: parameter("thickness_mm", 12.0),
            center_hole_diameter: parameter("center_hole_diameter_mm", 30.0),
            bolt_hole_diameter: parameter("bolt_hole_diameter_mm", 8.0),
            bolt_circle_diameter: parameter("bolt_circle_diameter_mm", 70.0),
        };
        let mut features = BTreeMap::new();
        let mut add = |name: &str, operation| {
            let id = Uuid::new_v4();
            features.insert(
                id,
                Feature {
                    id,
                    name: name.into(),
                    operation,
                },
            );
            id
        };
        let origin = || ScalarExpr::length_mm(0.0);
        let stock_profile = add(
            "stock_profile",
            Operation::Circle {
                radius: ScalarExpr::parameter(flange.outer_diameter, 0.5),
                center_x: origin(),
                center_y: origin(),
            },
        );
        let stock = add(
            "stock_extrude",
            Operation::Extrude {
                profile: stock_profile,
                distance: ScalarExpr::parameter(flange.thickness, 1.0),
            },
        );
        let center_profile = add(
            "center_hole_profile",
            Operation::Circle {
                radius: ScalarExpr::parameter(flange.center_hole_diameter, 0.5),
                center_x: origin(),
                center_y: origin(),
            },
        );
        let center_tool = add(
            "center_hole_extrude",
            Operation::Extrude {
                profile: center_profile,
                distance: ScalarExpr::parameter(flange.thickness, 1.0),
            },
        );
        let mut final_feature = add(
            "center_hole_cut",
            Operation::Cut {
                base: stock,
                tool: center_tool,
            },
        );
        for (index, (x, y)) in [(0.5, 0.0), (0.0, 0.5), (-0.5, 0.0), (0.0, -0.5)]
            .into_iter()
            .enumerate()
        {
            let center = |scale: f64| {
                if scale == 0.0 {
                    origin()
                } else {
                    ScalarExpr::parameter(flange.bolt_circle_diameter, scale)
                }
            };
            let profile = add(
                &format!("bolt_hole_{index}_profile"),
                Operation::Circle {
                    radius: ScalarExpr::parameter(flange.bolt_hole_diameter, 0.5),
                    center_x: center(x),
                    center_y: center(y),
                },
            );
            let tool = add(
                &format!("bolt_hole_{index}_extrude"),
                Operation::Extrude {
                    profile,
                    distance: ScalarExpr::parameter(flange.thickness, 1.0),
                },
            );
            final_feature = add(
                &format!("bolt_hole_{index}_cut"),
                Operation::Cut {
                    base: final_feature,
                    tool,
                },
            );
        }
        let root_node_id = Uuid::new_v4();
        let part = Uuid::new_v4();
        let project = Self {
            schema_version: SPEC_SCHEMA.into(),
            project_id: Uuid::new_v4(),
            name: name.into(),
            root_node_id,
            nodes: BTreeMap::from([
                (
                    root_node_id,
                    SceneNode {
                        id: root_node_id,
                        name: "World".into(),
                        parent: None,
                        feature: None,
                    },
                ),
                (
                    part,
                    SceneNode {
                        id: part,
                        name: "Flange".into(),
                        parent: Some(root_node_id),
                        feature: Some(final_feature),
                    },
                ),
            ]),
            parameters,
            features,
            flange: Some(flange),
            acceptance: None,
            precision: PrecisionProfile::default(),
        };
        project.validate()?;
        Ok(project)
    }
}
