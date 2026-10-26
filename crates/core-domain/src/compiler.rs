use std::collections::BTreeMap;

use uuid::Uuid;

use crate::*;

impl Project {
    pub fn compile(&self, revision_id: Uuid) -> Result<ExecutionPlan> {
        self.validate()?;
        if self.features.len() > 256 {
            return Err(CoreError::new(
                "CAPABILITY_UNAVAILABLE",
                "The current CAD evaluator supports at most 256 features",
            ));
        }
        crate::validation::valid_id(revision_id)?;
        let mut features = Vec::with_capacity(self.features.len());
        for id in self.topological_order()? {
            let definition = &self.features[&id];
            let (op, inputs, parameters) = match &definition.operation {
                Operation::Circle {
                    radius,
                    center_x,
                    center_y,
                } => (
                    "profile.circle@1",
                    BTreeMap::new(),
                    BTreeMap::from([
                        ("radius_mm".into(), self.resolve_length(radius)?),
                        ("center_x_mm".into(), self.resolve_length(center_x)?),
                        ("center_y_mm".into(), self.resolve_length(center_y)?),
                    ]),
                ),
                Operation::Extrude { profile, distance } => (
                    "solid.extrude@1",
                    BTreeMap::from([("profile".into(), *profile)]),
                    BTreeMap::from([("distance_mm".into(), self.resolve_length(distance)?)]),
                ),
                Operation::Cut { base, tool } => (
                    "solid.cut@1",
                    BTreeMap::from([("base".into(), *base), ("tool".into(), *tool)]),
                    BTreeMap::new(),
                ),
            };
            features.push(PlannedFeature {
                id,
                name: definition.name.clone(),
                op: op.into(),
                inputs,
                parameters,
            });
        }
        Ok(ExecutionPlan {
            schema_version: PLAN_SCHEMA.into(),
            project_id: self.project_id,
            revision_id,
            units: "mm".into(),
            coordinate_system: "right_handed_z_up".into(),
            features,
            outputs: self
                .nodes
                .values()
                .filter_map(|node| node.feature.map(|feature| (node.id, feature)))
                .collect(),
            acceptance: self.flange_spec()?,
            precision: self.precision.clone(),
        })
    }
}
