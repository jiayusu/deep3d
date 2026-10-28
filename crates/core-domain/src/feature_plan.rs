use crate::*;
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn xyz(parameters: &mut BTreeMap<String, f64>, prefix: &str, suffix: &str, values: [f64; 3]) {
    for (axis, value) in ["x", "y", "z"].into_iter().zip(values) {
        parameters.insert(format!("{prefix}{axis}{suffix}"), value);
    }
}

impl Project {
    fn lengths(&self, values: &[ScalarExpr; 3]) -> Result<[f64; 3]> {
        Ok([
            self.resolve_length(&values[0])?,
            self.resolve_length(&values[1])?,
            self.resolve_length(&values[2])?,
        ])
    }

    fn selector_plan(&self, selector: &EdgeSelector) -> Result<Value> {
        Ok(match selector {
            EdgeSelector::AllEdges { expected_count } => {
                json!({"kind":"all_edges","expected_count":expected_count})
            }
            EdgeSelector::LineParallel {
                axis,
                expected_count,
            } => json!({"kind":"line_parallel","axis":axis,"expected_count":expected_count}),
            EdgeSelector::Plane {
                axis,
                coordinate,
                expected_count,
            } => {
                json!({"kind":"plane","axis":axis,"coordinate_mm":self.resolve_length(coordinate)?,"expected_count":expected_count})
            }
            EdgeSelector::Circle {
                radius,
                axis,
                coordinate,
                expected_count,
            } => {
                json!({"kind":"circle","radius_mm":self.resolve_length(radius)?,"axis":axis,"coordinate_mm":self.resolve_length(coordinate)?,"expected_count":expected_count})
            }
            EdgeSelector::LineParallelInPlane {
                direction,
                axis,
                coordinate,
                expected_count,
            } => {
                json!({"kind":"line_parallel_in_plane","direction":direction,"axis":axis,"coordinate_mm":self.resolve_length(coordinate)?,"expected_count":expected_count})
            }
        })
    }

    pub(crate) fn compile_feature(&self, feature: &Feature) -> Result<PlannedFeature> {
        let mut inputs = BTreeMap::new();
        let mut parameters = BTreeMap::new();
        let mut data = BTreeMap::new();
        let op = match &feature.operation {
            Operation::Circle {
                radius,
                center_x,
                center_y,
            } => {
                parameters.insert("radius_mm".into(), self.resolve_length(radius)?);
                parameters.insert("center_x_mm".into(), self.resolve_length(center_x)?);
                parameters.insert("center_y_mm".into(), self.resolve_length(center_y)?);
                "profile.circle@1"
            }
            Operation::Rectangle {
                width,
                height,
                center_x,
                center_y,
            } => {
                parameters.insert("width_mm".into(), self.resolve_length(width)?);
                parameters.insert("height_mm".into(), self.resolve_length(height)?);
                parameters.insert("center_x_mm".into(), self.resolve_length(center_x)?);
                parameters.insert("center_y_mm".into(), self.resolve_length(center_y)?);
                "profile.rectangle@1"
            }
            Operation::Polygon { points } => {
                let points = points
                    .iter()
                    .map(|point| {
                        Ok([
                            self.resolve_length(&point[0])?,
                            self.resolve_length(&point[1])?,
                        ])
                    })
                    .collect::<Result<Vec<_>>>()?;
                data.insert("points_mm".into(), json!(points));
                "profile.polygon@1"
            }
            Operation::Sketch { sketch } => {
                data.insert("sketch".into(), self.compile_sketch(sketch)?);
                "profile.sketch@1"
            }
            Operation::Extrude { profile, distance } => {
                inputs.insert("profile".into(), *profile);
                parameters.insert("distance_mm".into(), self.resolve_length(distance)?);
                "solid.extrude@1"
            }
            Operation::Revolve {
                profile,
                angle,
                axis_origin,
                axis_direction,
            } => {
                inputs.insert("profile".into(), *profile);
                parameters.insert("angle_rad".into(), self.resolve_angle(angle)?);
                xyz(
                    &mut parameters,
                    "origin_",
                    "_mm",
                    self.lengths(axis_origin)?,
                );
                xyz(&mut parameters, "axis_", "", *axis_direction);
                "solid.revolve@1"
            }
            Operation::Cut { base, tool }
            | Operation::Fuse { base, tool }
            | Operation::Common { base, tool } => {
                inputs.insert("base".into(), *base);
                inputs.insert("tool".into(), *tool);
                match feature.operation {
                    Operation::Cut { .. } => "solid.cut@1",
                    Operation::Fuse { .. } => "solid.fuse@1",
                    _ => "solid.common@1",
                }
            }
            Operation::Transform {
                shape,
                translation,
                axis,
                angle,
            } => {
                inputs.insert("shape".into(), *shape);
                xyz(&mut parameters, "d", "_mm", self.lengths(translation)?);
                xyz(&mut parameters, "axis_", "", *axis);
                parameters.insert("angle_rad".into(), self.resolve_angle(angle)?);
                "shape.transform@1"
            }
            Operation::Fillet {
                shape,
                radius,
                selector,
            } => {
                inputs.insert("shape".into(), *shape);
                parameters.insert("radius_mm".into(), self.resolve_length(radius)?);
                data.insert("selector".into(), self.selector_plan(selector)?);
                "solid.fillet@1"
            }
            Operation::Chamfer {
                shape,
                distance,
                selector,
            } => {
                inputs.insert("shape".into(), *shape);
                parameters.insert("distance_mm".into(), self.resolve_length(distance)?);
                data.insert("selector".into(), self.selector_plan(selector)?);
                "solid.chamfer@1"
            }
            Operation::LinearPattern {
                shape,
                count,
                step,
                fuse,
            } => {
                inputs.insert("shape".into(), *shape);
                xyz(&mut parameters, "step_", "_mm", self.lengths(step)?);
                data.insert("count".into(), json!(count));
                data.insert("fuse".into(), json!(fuse));
                "shape.linear_pattern@1"
            }
            Operation::CircularPattern {
                shape,
                count,
                axis,
                origin,
                step_angle,
                fuse,
            } => {
                inputs.insert("shape".into(), *shape);
                xyz(&mut parameters, "origin_", "_mm", self.lengths(origin)?);
                xyz(&mut parameters, "axis_", "", *axis);
                parameters.insert("step_angle_rad".into(), self.resolve_angle(step_angle)?);
                data.insert("count".into(), json!(count));
                data.insert("fuse".into(), json!(fuse));
                "shape.circular_pattern@1"
            }
            Operation::Hole {
                base,
                diameter,
                depth,
                centers,
                start_z,
            } => {
                inputs.insert("base".into(), *base);
                parameters.insert("diameter_mm".into(), self.resolve_length(diameter)?);
                parameters.insert("depth_mm".into(), self.resolve_length(depth)?);
                parameters.insert("start_z_mm".into(), self.resolve_length(start_z)?);
                let centers = centers
                    .iter()
                    .map(|point| {
                        Ok([
                            self.resolve_length(&point[0])?,
                            self.resolve_length(&point[1])?,
                        ])
                    })
                    .collect::<Result<Vec<_>>>()?;
                data.insert("centers_mm".into(), json!(centers));
                "solid.hole@1"
            }
            Operation::Compound { parts } => {
                for (index, part) in parts.iter().enumerate() {
                    inputs.insert(format!("part_{index}"), *part);
                }
                "assembly.compound@1"
            }
        };
        Ok(PlannedFeature {
            id: feature.id,
            name: feature.name.clone(),
            op: op.into(),
            inputs,
            parameters,
            data,
        })
    }
}
