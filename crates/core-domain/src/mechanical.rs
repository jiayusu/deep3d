use crate::*;
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

fn invalid(message: &str) -> CoreError {
    CoreError::new("PARAMETER_INVALID", message)
}

impl NodeAcceptance {
    pub fn validate(&self) -> Result<()> {
        if !(1..=4096).contains(&self.solid_count)
            || self.volume_mm3.is_none() && self.bounds_mm.is_none()
        {
            return Err(CoreError::new(
                "ACCEPTANCE_INVALID",
                "Each output needs a solid count and independent volume or bounds target",
            ));
        }
        if self
            .volume_mm3
            .is_some_and(|volume| !volume.is_finite() || volume <= 0.0)
        {
            return Err(CoreError::new(
                "ACCEPTANCE_INVALID",
                "Expected volume must be finite and positive",
            ));
        }
        if let Some(bounds) = self.bounds_mm {
            if bounds
                .iter()
                .any(|value| !value.is_finite() || value.abs() > 10000.0)
                || (0..3).any(|i| bounds[i] >= bounds[i + 3])
            {
                return Err(CoreError::new(
                    "ACCEPTANCE_INVALID",
                    "Bounds require finite ordered min/max coordinates within +/-10000 mm",
                ));
            }
        }
        Ok(())
    }
}

impl GeneralAcceptance {
    pub fn new(nodes: BTreeMap<Uuid, NodeAcceptance>) -> Self {
        Self {
            schema_version: "deep3d.mechanical-acceptance/1".into(),
            nodes,
            assembly: None,
            max_interference_volume_mm3: None,
        }
    }

    pub fn validate(&self) -> Result<()> {
        if self.schema_version != "deep3d.mechanical-acceptance/1" {
            return Err(CoreError::new(
                "SCHEMA_UNSUPPORTED",
                "Unsupported mechanical acceptance contract",
            ));
        }
        if self.nodes.is_empty() || self.nodes.len() > 64 || self.nodes.keys().any(Uuid::is_nil) {
            return Err(CoreError::new(
                "ACCEPTANCE_INVALID",
                "Mechanical acceptance requires 1..64 valid node identities",
            ));
        }
        for expected in self.nodes.values().chain(self.assembly.iter()) {
            expected.validate()?;
        }
        if self
            .max_interference_volume_mm3
            .is_some_and(|value| !value.is_finite() || !(0.0..=1e12).contains(&value))
        {
            return Err(CoreError::new(
                "ACCEPTANCE_INVALID",
                "Interference limit must be finite, nonnegative and <=1e12 mm3",
            ));
        }
        Ok(())
    }
}

pub(crate) fn axis(axis: &[f64; 3]) -> Result<()> {
    if axis.iter().any(|x| !x.is_finite())
        || (axis.iter().map(|x| x * x).sum::<f64>().sqrt() - 1.0).abs() > 1e-9
    {
        return Err(invalid("Axis must be a finite unit vector"));
    }
    Ok(())
}

impl Project {
    pub fn new_mechanical(
        name: &str,
        parameters: BTreeMap<Uuid, Parameter>,
        features: BTreeMap<Uuid, Feature>,
        nodes: BTreeMap<Uuid, SceneNode>,
        root_node_id: Uuid,
        acceptance: GeneralAcceptance,
    ) -> Result<Self> {
        let project = Self {
            schema_version: SPEC_SCHEMA.into(),
            project_id: Uuid::new_v4(),
            name: name.into(),
            root_node_id,
            nodes,
            parameters,
            features,
            flange: None,
            acceptance: Some(acceptance),
            precision: PrecisionProfile::default(),
        };
        project.validate()?;
        Ok(project)
    }

    pub(crate) fn validate_acceptance(&self, contract: &GeneralAcceptance) -> Result<()> {
        contract.validate()?;
        let outputs: BTreeSet<_> = self
            .nodes
            .values()
            .filter(|node| node.feature.is_some())
            .map(|node| node.id)
            .collect();
        if contract.nodes.keys().copied().collect::<BTreeSet<_>>() != outputs {
            return Err(CoreError::new(
                "ACCEPTANCE_INVALID",
                "Acceptance nodes must exactly cover every Scene output",
            ));
        }
        for node in self.nodes.values().filter(|node| node.feature.is_some()) {
            if self
                .nodes
                .values()
                .any(|child| child.parent == Some(node.id))
            {
                return Err(CoreError::new(
                    "HIERARCHY_INVALID",
                    "Mechanical geometry bindings must be leaf nodes",
                ));
            }
        }
        for node in self.nodes.values() {
            let mut parent = node.parent;
            let mut depth = 0;
            while let Some(id) = parent {
                depth += 1;
                if depth > 64 {
                    return Err(CoreError::new(
                        "HIERARCHY_INVALID",
                        "Mechanical Scene depth exceeds 64",
                    ));
                }
                parent = self.nodes.get(&id).and_then(|node| node.parent);
            }
        }
        Ok(())
    }

    fn require(&self, id: Uuid, output_type: OutputType) -> Result<()> {
        let feature = self
            .features
            .get(&id)
            .ok_or_else(|| CoreError::new("FEATURE_NOT_FOUND", "Unknown feature input"))?;
        if feature.operation.output_type() != output_type {
            return Err(CoreError::new(
                "TYPE_MISMATCH",
                "Feature input has an incompatible output type",
            ));
        }
        Ok(())
    }

    fn coordinate(&self, expression: &ScalarExpr) -> Result<f64> {
        let value = self.resolve_length(expression)?;
        if value.abs() > 1000.0 {
            return Err(invalid("Coordinate exceeds +/-1000 mm"));
        }
        Ok(value)
    }

    fn positive(&self, expression: &ScalarExpr) -> Result<f64> {
        let value = self.resolve_length(expression)?;
        if !(0.0001..=1000.0).contains(&value) {
            return Err(invalid("Positive size requires 0.0001..1000 mm"));
        }
        Ok(value)
    }

    fn angle(&self, expression: &ScalarExpr) -> Result<f64> {
        let value = self.resolve_angle(expression)?;
        if value.abs() > std::f64::consts::TAU {
            return Err(invalid("Angle exceeds +/-2pi"));
        }
        Ok(value)
    }

    fn selector(&self, selector: &EdgeSelector) -> Result<()> {
        if !(1..=4096).contains(&selector.expected_count()) {
            return Err(invalid(
                "Edge selector needs an expected count within 1..4096",
            ));
        }
        match selector {
            EdgeSelector::AllEdges { .. } => {}
            EdgeSelector::LineParallel {
                axis: direction, ..
            } => axis(direction)?,
            EdgeSelector::Plane { coordinate, .. } => {
                self.coordinate(coordinate)?;
            }
            EdgeSelector::Circle {
                radius, coordinate, ..
            } => {
                self.positive(radius)?;
                self.coordinate(coordinate)?;
            }
            EdgeSelector::LineParallelInPlane {
                direction,
                coordinate,
                ..
            } => {
                axis(direction)?;
                self.coordinate(coordinate)?;
            }
        }
        Ok(())
    }

    pub(crate) fn validate_operation(&self, operation: &Operation) -> Result<()> {
        match operation {
            Operation::Circle {
                radius,
                center_x,
                center_y,
            } => {
                let radius = self.resolve_length(radius)?;
                let x = self.resolve_length(center_x)?;
                let y = self.resolve_length(center_y)?;
                if !(0.05..=500.0).contains(&radius) || x.abs() > 1000.0 || y.abs() > 1000.0 {
                    return Err(CoreError::new(
                        "OUTSIDE_QUALIFIED_ENVELOPE",
                        "Circle radius/center outside operation envelope",
                    ));
                }
            }
            Operation::Rectangle {
                width,
                height,
                center_x,
                center_y,
            } => {
                self.positive(width)?;
                self.positive(height)?;
                self.coordinate(center_x)?;
                self.coordinate(center_y)?;
            }
            Operation::Polygon { points } => {
                if !(3..=128).contains(&points.len()) {
                    return Err(invalid("Polygon needs 3..128 implicit-closure points"));
                }
                let points = points
                    .iter()
                    .map(|point| Ok([self.coordinate(&point[0])?, self.coordinate(&point[1])?]))
                    .collect::<Result<Vec<_>>>()?;
                validate_polygon(&points)?;
            }
            Operation::Sketch { sketch } => self.validate_sketch(sketch)?,
            Operation::Extrude { profile, distance } => {
                self.require(*profile, OutputType::PlanarProfile)?;
                if !(1.0..=1000.0).contains(&self.resolve_length(distance)?) {
                    return Err(CoreError::new(
                        "OUTSIDE_QUALIFIED_ENVELOPE",
                        "Extrusion must be within 1..1000 mm",
                    ));
                }
            }
            Operation::Revolve {
                profile,
                angle: rotation,
                axis_origin,
                axis_direction,
            } => {
                self.require(*profile, OutputType::PlanarProfile)?;
                let angle = self.angle(rotation)?;
                if angle < 1e-6 {
                    return Err(invalid("Revolution requires a positive angle"));
                }
                axis(axis_direction)?;
                for value in axis_origin {
                    self.coordinate(value)?;
                }
            }
            Operation::Cut { base, tool }
            | Operation::Fuse { base, tool }
            | Operation::Common { base, tool } => {
                self.require(*base, OutputType::BRepSolid)?;
                self.require(*tool, OutputType::BRepSolid)?;
                if matches!(operation, Operation::Cut { .. }) && base == tool {
                    return Err(invalid(
                        "Subtracting a feature from itself produces no solid",
                    ));
                }
            }
            Operation::Transform {
                shape,
                translation,
                axis: direction,
                angle: rotation,
            } => {
                self.require(*shape, OutputType::BRepSolid)?;
                axis(direction)?;
                self.angle(rotation)?;
                for value in translation {
                    self.coordinate(value)?;
                }
            }
            Operation::Fillet {
                shape,
                radius,
                selector,
            } => {
                self.require(*shape, OutputType::BRepSolid)?;
                self.positive(radius)?;
                self.selector(selector)?;
            }
            Operation::Chamfer {
                shape,
                distance,
                selector,
            } => {
                self.require(*shape, OutputType::BRepSolid)?;
                self.positive(distance)?;
                self.selector(selector)?;
            }
            Operation::LinearPattern {
                shape, count, step, ..
            } => {
                self.require(*shape, OutputType::BRepSolid)?;
                if !(2..=128).contains(count) {
                    return Err(invalid("Pattern count requires 2..128"));
                }
                let values = step
                    .iter()
                    .map(|value| self.coordinate(value))
                    .collect::<Result<Vec<_>>>()?;
                if values.iter().map(|value| value * value).sum::<f64>().sqrt() < 0.0001 {
                    return Err(invalid("Linear pattern step must be nonzero"));
                }
            }
            Operation::CircularPattern {
                shape,
                count,
                axis: direction,
                origin,
                step_angle,
                ..
            } => {
                self.require(*shape, OutputType::BRepSolid)?;
                axis(direction)?;
                if !(2..=128).contains(count) {
                    return Err(invalid("Pattern count requires 2..128"));
                }
                let step = self.angle(step_angle)?.abs();
                if step < 1e-6 || step * f64::from(count - 1) >= std::f64::consts::TAU - 1e-9 {
                    return Err(invalid("Circular pattern needs nonzero increments spanning less than one full turn"));
                }
                for value in origin {
                    self.coordinate(value)?;
                }
            }
            Operation::Hole {
                base,
                diameter,
                depth,
                centers,
                start_z,
            } => {
                self.require(*base, OutputType::BRepSolid)?;
                self.positive(diameter)?;
                self.positive(depth)?;
                self.coordinate(start_z)?;
                if centers.is_empty() || centers.len() > 128 {
                    return Err(invalid("Hole needs 1..128 explicit centers"));
                }
                let centers = centers
                    .iter()
                    .map(|center| Ok([self.coordinate(&center[0])?, self.coordinate(&center[1])?]))
                    .collect::<Result<Vec<_>>>()?;
                for (i, first) in centers.iter().enumerate() {
                    if centers
                        .iter()
                        .skip(i + 1)
                        .any(|second| distance(*first, *second) < 0.0001)
                    {
                        return Err(invalid("Hole centers must be distinct"));
                    }
                }
            }
            Operation::Compound { parts } => {
                if parts.is_empty() || parts.len() > 256 {
                    return Err(invalid("Compound needs 1..256 part occurrences"));
                }
                for part in parts {
                    self.require(*part, OutputType::BRepSolid)?;
                }
            }
        }
        Ok(())
    }

    pub(crate) fn validate_geometry_budget(&self) -> Result<()> {
        let mut costs = BTreeMap::<Uuid, usize>::new();
        let mut total = 0usize;
        for id in self.topological_order()? {
            let operation = &self.features[&id].operation;
            let mut cost = operation
                .dependencies()
                .iter()
                .map(|input| costs[input])
                .sum::<usize>()
                .max(1);
            match operation {
                Operation::Compound { parts } => {
                    cost = parts.iter().map(|part| costs[part]).sum();
                }
                Operation::Rectangle { .. } => {
                    cost = 4;
                }
                Operation::Polygon { points } => {
                    cost = points.len();
                }
                Operation::Sketch { sketch } => {
                    cost = sketch.lines.len() + sketch.circles.len() + sketch.arcs.len();
                }
                Operation::LinearPattern { count, .. }
                | Operation::CircularPattern { count, .. } => {
                    cost = cost.saturating_mul(*count as usize);
                }
                Operation::Hole { centers, .. } => {
                    cost = cost.saturating_add(centers.len());
                }
                _ => {}
            }
            total = total.saturating_add(cost);
            if cost > 4096 || total > 32768 {
                return Err(CoreError::new(
                    "PROJECT_LIMIT",
                    "Nested operations exceed the 4096 per-feature/32768 total geometry budget",
                ));
            }
            costs.insert(id, cost);
        }
        let output_cost: usize = self
            .nodes
            .values()
            .filter_map(|node| node.feature)
            .map(|id| costs[&id])
            .sum();
        if output_cost > 4096 {
            return Err(CoreError::new(
                "PROJECT_LIMIT",
                "Scene occurrences exceed the 4096 aggregate geometry budget",
            ));
        }
        Ok(())
    }
}

fn distance(a: [f64; 2], b: [f64; 2]) -> f64 {
    (a[0] - b[0]).hypot(a[1] - b[1])
}
fn cross(a: [f64; 2], b: [f64; 2], c: [f64; 2]) -> f64 {
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
}
fn on_segment(a: [f64; 2], b: [f64; 2], p: [f64; 2]) -> bool {
    cross(a, b, p).abs() <= 1e-9
        && (0..2).all(|i| p[i] >= a[i].min(b[i]) - 1e-9 && p[i] <= a[i].max(b[i]) + 1e-9)
}
fn intersects(a: [f64; 2], b: [f64; 2], c: [f64; 2], d: [f64; 2]) -> bool {
    let (ab_c, ab_d, cd_a, cd_b) = (
        cross(a, b, c),
        cross(a, b, d),
        cross(c, d, a),
        cross(c, d, b),
    );
    (ab_c * ab_d < -1e-18 && cd_a * cd_b < -1e-18)
        || on_segment(a, b, c)
        || on_segment(a, b, d)
        || on_segment(c, d, a)
        || on_segment(c, d, b)
}
fn validate_polygon(points: &[[f64; 2]]) -> Result<()> {
    let n = points.len();
    let area: f64 = (0..n)
        .map(|i| points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1])
        .sum();
    if area.abs() < 1e-8 || (0..n).any(|i| distance(points[i], points[(i + 1) % n]) < 0.0001) {
        return Err(invalid(
            "Polygon needs nonzero area and nondegenerate edges",
        ));
    }
    for i in 0..n {
        for j in (i + 1)..n {
            if j == i + 1 || i == 0 && j == n - 1 {
                continue;
            }
            if intersects(
                points[i],
                points[(i + 1) % n],
                points[j],
                points[(j + 1) % n],
            ) {
                return Err(invalid(
                    "Polygon edges must not intersect or touch nonadjacent edges",
                ));
            }
        }
    }
    Ok(())
}
