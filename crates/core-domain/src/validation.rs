use std::collections::{BTreeMap, BTreeSet};

use uuid::Uuid;

use crate::*;

pub(crate) fn finite(value: f64, field: &str) -> Result<f64> {
    if !value.is_finite() {
        return Err(CoreError::new(
            "PARAMETER_INVALID",
            format!("{field} must be finite"),
        ));
    }
    Ok(value)
}

pub(crate) fn valid_id(id: Uuid) -> Result<()> {
    if id.is_nil() {
        return Err(CoreError::new(
            "ID_INVALID",
            "Nil UUID is not a persistent identity",
        ));
    }
    Ok(())
}

pub(crate) fn valid_name(name: &str) -> Result<()> {
    if name.trim().is_empty() || name.chars().count() > 256 || name.chars().any(char::is_control) {
        return Err(CoreError::new(
            "NAME_INVALID",
            "Names need 1..256 non-control characters",
        ));
    }
    Ok(())
}

impl FlangeSpec {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != "deep3d.flange/1"
            || self.units != "mm"
            || self.coordinate_system != "right_handed_z_up"
        {
            return Err(CoreError::new(
                "SCHEMA_UNSUPPORTED",
                "Flange requires v1 mm/right_handed_z_up",
            ));
        }
        for (name, value, lower) in [
            ("outer_diameter_mm", self.outer_diameter_mm, 1.0),
            ("thickness_mm", self.thickness_mm, 1.0),
            ("center_hole_diameter_mm", self.center_hole_diameter_mm, 0.1),
            ("bolt_hole_diameter_mm", self.bolt_hole_diameter_mm, 0.1),
            ("bolt_circle_diameter_mm", self.bolt_circle_diameter_mm, 0.1),
        ] {
            finite(value, name)?;
            if !(lower..=1000.0).contains(&value) {
                return Err(CoreError::new(
                    "OUTSIDE_QUALIFIED_ENVELOPE",
                    format!("{name} outside {lower}..1000 mm"),
                ));
            }
        }
        finite(self.start_angle_rad, "start_angle_rad")?;
        if !(3..=32).contains(&self.hole_count)
            || self.start_angle_rad.abs() > std::f64::consts::TAU
        {
            return Err(CoreError::new(
                "OUTSIDE_QUALIFIED_ENVELOPE",
                "Hole count must be 3..32 and angle within +/-2pi",
            ));
        }
        let stock = self.outer_diameter_mm / 2.0;
        let pitch = self.bolt_circle_diameter_mm / 2.0;
        let radius = self.bolt_hole_diameter_mm / 2.0;
        let walls = [
            stock - pitch - radius,
            pitch - self.center_hole_diameter_mm / 2.0 - radius,
            2.0 * pitch * (std::f64::consts::PI / f64::from(self.hole_count)).sin()
                - self.bolt_hole_diameter_mm,
        ];
        if walls.iter().any(|wall| *wall < 0.1 - 1e-12) {
            return Err(CoreError::new(
                "PARAMETER_INVALID",
                "Holes require at least 0.1 mm material ligaments",
            ));
        }
        Ok(())
    }

    pub fn expected_volume_mm3(&self) -> f64 {
        std::f64::consts::PI
            * ((self.outer_diameter_mm / 2.0).powi(2)
                - (self.center_hole_diameter_mm / 2.0).powi(2)
                - f64::from(self.hole_count) * (self.bolt_hole_diameter_mm / 2.0).powi(2))
            * self.thickness_mm
    }
}

impl PrecisionProfile {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != "deep3d.precision/1" {
            return Err(CoreError::new(
                "SCHEMA_UNSUPPORTED",
                "Unsupported precision profile",
            ));
        }
        for (name, value) in [
            ("working_tolerance_mm", self.working_tolerance_mm),
            ("max_subshape_tolerance_mm", self.max_subshape_tolerance_mm),
            (
                "critical_dimension_error_mm",
                self.critical_dimension_error_mm,
            ),
            (
                "step_roundtrip_dimension_error_mm",
                self.step_roundtrip_dimension_error_mm,
            ),
            ("volume_relative_error", self.volume_relative_error),
            (
                "measurement_error_budget_mm",
                self.measurement_error_budget_mm,
            ),
        ] {
            finite(value, name)?;
            if value <= 0.0 {
                return Err(CoreError::new(
                    "PARAMETER_INVALID",
                    format!("{name} must be positive"),
                ));
            }
        }
        if self.working_tolerance_mm > 0.001 {
            return Err(CoreError::new(
                "OUTSIDE_QUALIFIED_ENVELOPE",
                "Working tolerance must be <=0.001 mm",
            ));
        }
        if self.working_tolerance_mm > self.max_subshape_tolerance_mm
            || self.measurement_error_budget_mm
                >= self
                    .critical_dimension_error_mm
                    .min(self.step_roundtrip_dimension_error_mm)
            || self.volume_relative_error >= 1.0
        {
            return Err(CoreError::new(
                "PARAMETER_INVALID",
                "Incompatible precision thresholds",
            ));
        }
        Ok(())
    }
}

impl Project {
    pub(crate) fn resolve_length(&self, expression: &ScalarExpr) -> Result<f64> {
        self.resolve_scalar(expression, ScalarUnit::LengthMm)
    }

    pub(crate) fn resolve_angle(&self, expression: &ScalarExpr) -> Result<f64> {
        self.resolve_scalar(expression, ScalarUnit::AngleRad)
    }

    fn resolve_scalar(&self, expression: &ScalarExpr, unit: ScalarUnit) -> Result<f64> {
        if let ScalarExpr::Linear { terms, offset } = expression {
            if terms.is_empty() || terms.len() > 16 {
                return Err(CoreError::new(
                    "SCHEMA_INVALID",
                    "Linear expression needs 1..16 parameter terms",
                ));
            }
            let mut total = self.resolve_scalar(
                &ScalarExpr::Literal {
                    value: offset.clone(),
                },
                unit,
            )?;
            for term in terms {
                total += self
                    .resolve_scalar(&ScalarExpr::parameter(term.parameter_id, term.scale), unit)?;
            }
            return finite(total, "resolved linear expression");
        }
        let (value, scale) = match expression {
            ScalarExpr::Literal { value } => (value, 1.0),
            ScalarExpr::Parameter {
                parameter_id,
                scale,
            } => {
                let parameter = self.parameters.get(parameter_id).ok_or_else(|| {
                    CoreError::new(
                        "PARAMETER_NOT_FOUND",
                        format!("Unknown parameter {parameter_id}"),
                    )
                })?;
                (&parameter.value, finite(*scale, "parameter scale")?)
            }
            ScalarExpr::Linear { .. } => unreachable!("linear expressions resolve above"),
        };
        if value.unit() != unit {
            return Err(CoreError::new(
                "TYPE_MISMATCH",
                format!("Operation expects a {unit:?} expression"),
            ));
        }
        finite(value.number() * scale, "resolved length")
    }

    pub fn flange_spec(&self) -> Result<FlangeSpec> {
        let flange = self.flange.as_ref().ok_or_else(|| {
            CoreError::new(
                "TYPE_MISMATCH",
                "Project uses general mechanical acceptance",
            )
        })?;
        let length = |id| self.resolve_length(&ScalarExpr::parameter(id, 1.0));
        let result = FlangeSpec {
            outer_diameter_mm: length(flange.outer_diameter)?,
            thickness_mm: length(flange.thickness)?,
            center_hole_diameter_mm: length(flange.center_hole_diameter)?,
            bolt_hole_diameter_mm: length(flange.bolt_hole_diameter)?,
            bolt_circle_diameter_mm: length(flange.bolt_circle_diameter)?,
            ..FlangeSpec::default()
        };
        result.validate()?;
        Ok(result)
    }

    pub fn validate(&self) -> Result<()> {
        if self.schema_version != SPEC_SCHEMA {
            return Err(CoreError::new(
                "SCHEMA_UNSUPPORTED",
                "Unsupported project specification",
            ));
        }
        valid_id(self.project_id)?;
        valid_id(self.root_node_id)?;
        valid_name(&self.name)?;
        if self.nodes.is_empty()
            || self.features.is_empty()
            || self.nodes.len() > 4096
            || self.features.len() > 4096
            || self.parameters.len() > 4096
        {
            return Err(CoreError::new(
                "PROJECT_LIMIT",
                "Project needs nodes/features within the 4096 record limits",
            ));
        }
        self.precision.validate()?;
        let mut identities = BTreeSet::from([self.project_id]);
        let mut add_id = |key, record_id| -> Result<()> {
            valid_id(key)?;
            if key != record_id {
                return Err(CoreError::new(
                    "ID_MISMATCH",
                    "Map key differs from record identity",
                ));
            }
            if !identities.insert(key) {
                return Err(CoreError::new(
                    "ID_COLLISION",
                    "Identities must be unique across the project",
                ));
            }
            Ok(())
        };
        let mut parameter_names = BTreeSet::new();
        for (key, parameter) in &self.parameters {
            add_id(*key, parameter.id)?;
            valid_name(&parameter.name)?;
            finite(parameter.value.number(), "parameter value")?;
            if !parameter_names.insert(&parameter.name) {
                return Err(CoreError::new(
                    "NAME_CONFLICT",
                    "Parameter names must be unique",
                ));
            }
        }
        for (key, node) in &self.nodes {
            add_id(*key, node.id)?;
            valid_name(&node.name)?;
            if node.id == self.root_node_id {
                if node.parent.is_some() || node.feature.is_some() {
                    return Err(CoreError::new(
                        "HIERARCHY_INVALID",
                        "Root must have no parent or geometry binding",
                    ));
                }
            } else if node.parent.is_none() {
                return Err(CoreError::new(
                    "HIERARCHY_INVALID",
                    "Only declared root may have no parent",
                ));
            }
            if let Some(parent) = node.parent {
                if !self.nodes.contains_key(&parent) {
                    return Err(CoreError::new(
                        "NODE_NOT_FOUND",
                        format!("Unknown parent {parent}"),
                    ));
                }
            }
            if let Some(feature) = node.feature {
                let definition = self.features.get(&feature).ok_or_else(|| {
                    CoreError::new(
                        "FEATURE_NOT_FOUND",
                        format!("Unknown node feature {feature}"),
                    )
                })?;
                if definition.operation.output_type() != OutputType::BRepSolid {
                    return Err(CoreError::new(
                        "TYPE_MISMATCH",
                        "Scene geometry binding expects BRepSolid",
                    ));
                }
            }
        }
        if !self.nodes.contains_key(&self.root_node_id) {
            return Err(CoreError::new(
                "NODE_NOT_FOUND",
                "Declared root node is missing",
            ));
        }
        for id in self.nodes.keys() {
            let mut seen = BTreeSet::new();
            let mut current = Some(*id);
            while let Some(node) = current {
                if !seen.insert(node) {
                    return Err(CoreError::new(
                        "HIERARCHY_CYCLE",
                        "Scene hierarchy contains a cycle",
                    ));
                }
                current = self.nodes.get(&node).and_then(|record| record.parent);
            }
            if !seen.contains(&self.root_node_id) {
                return Err(CoreError::new(
                    "HIERARCHY_INVALID",
                    "Node does not reach declared root",
                ));
            }
        }
        let mut bound_outputs = 0;
        for node in self.nodes.values() {
            bound_outputs += usize::from(node.feature.is_some());
        }
        if bound_outputs == 0 {
            return Err(CoreError::new(
                "OUTPUT_MISSING",
                "Project needs at least one solid output binding",
            ));
        }
        match (&self.flange, &self.acceptance) {
            (Some(flange), None) => {
                let bound = [
                    flange.outer_diameter,
                    flange.thickness,
                    flange.center_hole_diameter,
                    flange.bolt_hole_diameter,
                    flange.bolt_circle_diameter,
                ];
                if bound.into_iter().collect::<BTreeSet<_>>().len() != 5 {
                    return Err(CoreError::new(
                        "PARAMETER_INVALID",
                        "Flange dimensions need distinct parameter identities",
                    ));
                }
                self.flange_spec()?;
            }
            (None, Some(acceptance)) => self.validate_acceptance(acceptance)?,
            _ => {
                return Err(CoreError::new(
                    "ACCEPTANCE_INVALID",
                    "Project needs exactly one flange or mechanical acceptance contract",
                ))
            }
        }
        for (key, feature) in &self.features {
            add_id(*key, feature.id)?;
            valid_name(&feature.name)?;
            self.validate_operation(&feature.operation)?;
        }
        self.validate_geometry_budget()?;
        Ok(())
    }

    pub fn topological_order(&self) -> Result<Vec<Uuid>> {
        let mut degrees: BTreeMap<Uuid, usize> = BTreeMap::new();
        let mut reverse: BTreeMap<Uuid, Vec<Uuid>> = BTreeMap::new();
        for (id, feature) in &self.features {
            let dependencies = feature.operation.dependencies();
            degrees.insert(*id, dependencies.len());
            for input in dependencies {
                if !self.features.contains_key(&input) {
                    return Err(CoreError::new(
                        "FEATURE_NOT_FOUND",
                        format!("Unknown dependency {input}"),
                    ));
                }
                reverse.entry(input).or_default().push(*id);
            }
        }
        let mut ready: BTreeSet<Uuid> = degrees
            .iter()
            .filter_map(|(id, degree)| (*degree == 0).then_some(*id))
            .collect();
        let mut order = Vec::with_capacity(self.features.len());
        while let Some(id) = ready.pop_first() {
            order.push(id);
            if let Some(dependents) = reverse.get(&id) {
                for dependent in dependents {
                    let degree = degrees.get_mut(dependent).expect("known graph vertex");
                    *degree -= 1;
                    if *degree == 0 {
                        ready.insert(*dependent);
                    }
                }
            }
        }
        if order.len() != self.features.len() {
            return Err(CoreError::new(
                "DEPENDENCY_CYCLE",
                "Feature dependency graph contains a cycle",
            ));
        }
        Ok(order)
    }
}
