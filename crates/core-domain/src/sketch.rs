use crate::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SketchCircle {
    pub center: String,
    pub radius: ScalarExpr,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SketchArc {
    pub center: String,
    pub start: String,
    pub end: String,
    pub ccw: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SketchEdgeRef {
    pub entity: String,
    pub reversed: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum SketchConstraint {
    Fix {
        id: String,
        point: String,
        x: ScalarExpr,
        y: ScalarExpr,
    },
    Horizontal {
        id: String,
        line: String,
    },
    Vertical {
        id: String,
        line: String,
    },
    Distance {
        id: String,
        a: String,
        b: String,
        value: ScalarExpr,
    },
    Coincident {
        id: String,
        a: String,
        b: String,
    },
    Parallel {
        id: String,
        a: String,
        b: String,
    },
    Perpendicular {
        id: String,
        a: String,
        b: String,
    },
    EqualLength {
        id: String,
        a: String,
        b: String,
    },
    Angle {
        id: String,
        a: String,
        b: String,
        value: ScalarExpr,
    },
    Diameter {
        id: String,
        circle: String,
        value: ScalarExpr,
    },
    PointOnLine {
        id: String,
        point: String,
        line: String,
    },
    PointOnCircle {
        id: String,
        point: String,
        circle: String,
    },
    Tangent {
        id: String,
        a: String,
        b: String,
    },
    Midpoint {
        id: String,
        point: String,
        line: String,
    },
    SymmetricH {
        id: String,
        a: String,
        b: String,
    },
    SymmetricV {
        id: String,
        a: String,
        b: String,
    },
}

impl SketchConstraint {
    fn id(&self) -> &str {
        match self {
            Self::Fix { id, .. }
            | Self::Horizontal { id, .. }
            | Self::Vertical { id, .. }
            | Self::Distance { id, .. }
            | Self::Coincident { id, .. }
            | Self::Parallel { id, .. }
            | Self::Perpendicular { id, .. }
            | Self::EqualLength { id, .. }
            | Self::Angle { id, .. }
            | Self::Diameter { id, .. }
            | Self::PointOnLine { id, .. }
            | Self::PointOnCircle { id, .. }
            | Self::Tangent { id, .. }
            | Self::Midpoint { id, .. }
            | Self::SymmetricH { id, .. }
            | Self::SymmetricV { id, .. } => id,
        }
    }

    fn expressions(&self) -> Vec<&ScalarExpr> {
        match self {
            Self::Fix { x, y, .. } => vec![x, y],
            Self::Distance { value, .. }
            | Self::Angle { value, .. }
            | Self::Diameter { value, .. } => vec![value],
            _ => vec![],
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SketchDefinition {
    pub points: BTreeMap<String, [ScalarExpr; 2]>,
    pub lines: BTreeMap<String, [String; 2]>,
    pub circles: BTreeMap<String, SketchCircle>,
    pub arcs: BTreeMap<String, SketchArc>,
    pub constraints: Vec<SketchConstraint>,
    /// First loop is the outer wire; later loops are holes.
    pub loops: Vec<Vec<SketchEdgeRef>>,
}

impl SketchDefinition {
    pub(crate) fn expressions(&self) -> Vec<&ScalarExpr> {
        self.points
            .values()
            .flat_map(|point| point.iter())
            .chain(self.circles.values().map(|circle| &circle.radius))
            .chain(
                self.constraints
                    .iter()
                    .flat_map(SketchConstraint::expressions),
            )
            .collect()
    }
}

fn key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}
fn invalid(message: &str) -> CoreError {
    CoreError::new("SKETCH_INVALID", message)
}

impl Project {
    pub(crate) fn validate_sketch(&self, sketch: &SketchDefinition) -> Result<()> {
        let entities = sketch.lines.len() + sketch.circles.len() + sketch.arcs.len();
        if sketch.points.is_empty()
            || sketch.points.len() > 64
            || entities == 0
            || entities > 128
            || sketch.constraints.is_empty()
            || sketch.constraints.len() > 256
            || sketch.loops.is_empty()
            || sketch.loops.len() > 32
        {
            return Err(invalid(
                "Sketch limits: 1..64 points, 1..128 entities, 1..256 constraints and 1..32 loops",
            ));
        }
        let mut names = BTreeSet::new();
        for name in sketch
            .points
            .keys()
            .chain(sketch.lines.keys())
            .chain(sketch.circles.keys())
            .chain(sketch.arcs.keys())
            .map(String::as_str)
            .chain(sketch.constraints.iter().map(SketchConstraint::id))
        {
            if !key(name) || !names.insert(name) {
                return Err(invalid(
                    "Sketch local names must be unique bounded ASCII identities",
                ));
            }
        }
        let length = |value: &ScalarExpr, positive: bool| -> Result<()> {
            let value = self.resolve_length(value)?;
            if (!positive && value.abs() > 1000.0) || positive && !(0.05..=500.0).contains(&value) {
                return Err(invalid(
                    "Sketch coordinate/length exceeds its bounded domain",
                ));
            }
            Ok(())
        };
        let point = |name: &str| -> Result<()> {
            if !sketch.points.contains_key(name) {
                return Err(invalid("Unknown sketch point"));
            }
            Ok(())
        };
        let line = |name: &str| -> Result<()> {
            if !sketch.lines.contains_key(name) {
                return Err(invalid("Constraint requires a line"));
            }
            Ok(())
        };
        let circle = |name: &str| -> Result<()> {
            if !sketch.circles.contains_key(name) {
                return Err(invalid("Constraint requires a circle"));
            }
            Ok(())
        };
        let entity = |name: &str| -> bool {
            sketch.lines.contains_key(name)
                || sketch.circles.contains_key(name)
                || sketch.arcs.contains_key(name)
        };
        for coordinates in sketch.points.values() {
            for value in coordinates {
                length(value, false)?;
            }
        }
        for ends in sketch.lines.values() {
            point(&ends[0])?;
            point(&ends[1])?;
            if ends[0] == ends[1] {
                return Err(invalid("Line needs distinct endpoint identities"));
            }
            let first = &sketch.points[&ends[0]];
            let second = &sketch.points[&ends[1]];
            if (self.resolve_length(&first[0])? - self.resolve_length(&second[0])?)
                .hypot(self.resolve_length(&first[1])? - self.resolve_length(&second[1])?)
                <= 1e-6
            {
                return Err(invalid(
                    "Line seed endpoints must be farther than 1e-6 mm apart",
                ));
            }
        }
        for value in sketch.circles.values() {
            point(&value.center)?;
            length(&value.radius, true)?;
        }
        for arc in sketch.arcs.values() {
            for value in [&arc.center, &arc.start, &arc.end] {
                point(value)?;
            }
            if arc.center == arc.start || arc.center == arc.end || arc.start == arc.end {
                return Err(invalid(
                    "Arc needs distinct center, start and end point identities",
                ));
            }
            let coordinates = |name: &str| -> Result<[f64; 2]> {
                Ok([
                    self.resolve_length(&sketch.points[name][0])?,
                    self.resolve_length(&sketch.points[name][1])?,
                ])
            };
            let center = coordinates(&arc.center)?;
            let start = coordinates(&arc.start)?;
            let end = coordinates(&arc.end)?;
            for point in [start, end] {
                let radius = (point[0] - center[0]).hypot(point[1] - center[1]);
                if !(0.05..=500.0).contains(&radius) {
                    return Err(invalid("Arc seed radius requires 0.05..500 mm"));
                }
            }
            if (start[0] - end[0]).hypot(start[1] - end[1]) <= 1e-6 {
                return Err(invalid("Use a circle for a full circular loop"));
            }
        }
        for constraint in &sketch.constraints {
            match constraint {
                SketchConstraint::Fix {
                    point: name, x, y, ..
                } => {
                    point(name)?;
                    length(x, false)?;
                    length(y, false)?;
                }
                SketchConstraint::Horizontal { line: name, .. }
                | SketchConstraint::Vertical { line: name, .. } => line(name)?,
                SketchConstraint::Distance { a, b, value, .. } => {
                    point(a)?;
                    point(b)?;
                    if !(0.00001..=2000.0).contains(&self.resolve_length(value)?) {
                        return Err(invalid("Sketch distance requires 1e-5..2000 mm"));
                    }
                }
                SketchConstraint::Coincident { a, b, .. }
                | SketchConstraint::SymmetricH { a, b, .. }
                | SketchConstraint::SymmetricV { a, b, .. } => {
                    point(a)?;
                    point(b)?;
                }
                SketchConstraint::Parallel { a, b, .. }
                | SketchConstraint::Perpendicular { a, b, .. }
                | SketchConstraint::EqualLength { a, b, .. } => {
                    line(a)?;
                    line(b)?;
                }
                SketchConstraint::Angle { a, b, value, .. } => {
                    line(a)?;
                    line(b)?;
                    let angle = self.resolve_angle(value)?;
                    if angle <= 0.0 || angle >= std::f64::consts::PI {
                        return Err(invalid("Unsigned sketch line angle must be within (0,pi)"));
                    }
                }
                SketchConstraint::Diameter {
                    circle: name,
                    value,
                    ..
                } => {
                    circle(name)?;
                    if !(0.1..=1000.0).contains(&self.resolve_length(value)?) {
                        return Err(invalid("Sketch diameter requires 0.1..1000 mm"));
                    }
                }
                SketchConstraint::PointOnLine {
                    point: name,
                    line: edge,
                    ..
                }
                | SketchConstraint::Midpoint {
                    point: name,
                    line: edge,
                    ..
                } => {
                    point(name)?;
                    line(edge)?;
                }
                SketchConstraint::PointOnCircle {
                    point: name,
                    circle: curve,
                    ..
                } => {
                    point(name)?;
                    circle(curve)?;
                }
                SketchConstraint::Tangent { a, b, .. } => {
                    if !entity(a) || !entity(b) || a == b {
                        return Err(invalid("Tangent needs two existing curve entities"));
                    }
                    let ends = |name: &str| -> Option<[&str; 2]> {
                        if let Some(line) = sketch.lines.get(name) {
                            Some([&line[0], &line[1]])
                        } else {
                            sketch
                                .arcs
                                .get(name)
                                .map(|arc| [arc.start.as_str(), arc.end.as_str()])
                        }
                    };
                    if !sketch.arcs.contains_key(a) && !sketch.arcs.contains_key(b) {
                        return Err(invalid("Tangent supports only arc/line or arc/arc pairs"));
                    }
                    let (Some(first), Some(second)) = (ends(a), ends(b)) else {
                        return Err(invalid("Tangent supports only arc/line or arc/arc pairs"));
                    };
                    if first.iter().filter(|name| second.contains(name)).count() != 1 {
                        return Err(invalid(
                            "Tangent curves must share exactly one named endpoint",
                        ));
                    }
                }
            }
        }
        let mut used = BTreeSet::new();
        for edges in &sketch.loops {
            if edges.is_empty() || edges.len() > 128 {
                return Err(invalid("Each loop needs 1..128 edges"));
            }
            for edge in edges {
                if !entity(&edge.entity) || !used.insert(&edge.entity) {
                    return Err(invalid("Loop entities must exist and be used at most once"));
                }
            }
        }
        // Unreferenced fully constrained entities remain construction geometry.
        Ok(())
    }

    pub(crate) fn compile_sketch(&self, sketch: &SketchDefinition) -> Result<Value> {
        let points = sketch
            .points
            .iter()
            .map(|(name, xy)| {
                Ok((
                    name.clone(),
                    json!([self.resolve_length(&xy[0])?, self.resolve_length(&xy[1])?]),
                ))
            })
            .collect::<Result<BTreeMap<_, _>>>()?;
        let circles = sketch
            .circles
            .iter()
            .map(|(name, circle)| {
                Ok((
                    name.clone(),
                    json!({
            "center":circle.center,"radius_mm":self.resolve_length(&circle.radius)?}),
                ))
            })
            .collect::<Result<BTreeMap<_, _>>>()?;
        let constraints = sketch.constraints.iter().map(|constraint| -> Result<Value> {
            Ok(match constraint {
                SketchConstraint::Fix {id,point,x,y} => json!({"kind":"fix","id":id,"point":point,"x_mm":self.resolve_length(x)?,"y_mm":self.resolve_length(y)?}),
                SketchConstraint::Distance {id,a,b,value} => json!({"kind":"distance","id":id,"a":a,"b":b,"value_mm":self.resolve_length(value)?}),
                SketchConstraint::Angle {id,a,b,value} => json!({"kind":"angle","id":id,"a":a,"b":b,"value_rad":self.resolve_angle(value)?}),
                SketchConstraint::Diameter {id,circle,value} => json!({"kind":"diameter","id":id,"circle":circle,"value_mm":self.resolve_length(value)?}),
                _ => serde_json::to_value(constraint).map_err(|_| invalid("Cannot compile sketch constraint"))?,
            })
        }).collect::<Result<Vec<_>>>()?;
        Ok(
            json!({"points":points,"lines":sketch.lines,"circles":circles,"arcs":sketch.arcs,"constraints":constraints,"loops":sketch.loops}),
        )
    }
}
