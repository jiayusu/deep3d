use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::{CoreError, Result};

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
    Literal { value: ScalarValue },
    Parameter { parameter_id: Uuid, scale: f64 },
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

    pub(crate) fn parameter_ids(&self) -> BTreeSet<Uuid> {
        match self {
            Self::Literal { .. } => BTreeSet::new(),
            Self::Parameter { parameter_id, .. } => BTreeSet::from([*parameter_id]),
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
    #[serde(rename = "solid.extrude@1")]
    Extrude { profile: Uuid, distance: ScalarExpr },
    #[serde(rename = "solid.cut@1")]
    Cut { base: Uuid, tool: Uuid },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputType {
    PlanarProfile,
    BRepSolid,
}

impl Operation {
    pub fn output_type(&self) -> OutputType {
        match self {
            Self::Circle { .. } => OutputType::PlanarProfile,
            _ => OutputType::BRepSolid,
        }
    }

    pub fn dependencies(&self) -> BTreeSet<Uuid> {
        match self {
            Self::Circle { .. } => BTreeSet::new(),
            Self::Extrude { profile, .. } => BTreeSet::from([*profile]),
            Self::Cut { base, tool } => BTreeSet::from([*base, *tool]),
        }
    }

    pub fn parameter_ids(&self) -> BTreeSet<Uuid> {
        let expressions: Vec<&ScalarExpr> = match self {
            Self::Circle {
                radius,
                center_x,
                center_y,
            } => vec![radius, center_x, center_y],
            Self::Extrude { distance, .. } => vec![distance],
            Self::Cut { .. } => vec![],
        };
        expressions
            .into_iter()
            .flat_map(ScalarExpr::parameter_ids)
            .collect()
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
    pub flange: FlangeBinding,
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
    pub acceptance: FlangeSpec,
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
            flange,
            precision: PrecisionProfile::default(),
        };
        project.validate()?;
        Ok(project)
    }
}
