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
            features.push(self.compile_feature(&self.features[&id])?);
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
            scene: if self.acceptance.is_some() {
                self.nodes.clone()
            } else {
                BTreeMap::new()
            },
            acceptance: match &self.acceptance {
                Some(value) => Acceptance::Mechanical(value.clone()),
                None => Acceptance::Flange(self.flange_spec()?),
            },
            precision: self.precision.clone(),
        })
    }
}
