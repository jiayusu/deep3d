use std::collections::{BTreeMap, BTreeSet};

use uuid::Uuid;

use crate::*;

pub fn apply_transaction(project: &Project, transaction: &Transaction) -> Result<Project> {
    project.validate()?;
    if transaction.schema_version != TRANSACTION_SCHEMA {
        return Err(CoreError::new(
            "SCHEMA_UNSUPPORTED",
            "Unsupported transaction schema",
        ));
    }
    crate::validation::valid_id(transaction.id)?;
    crate::validation::valid_id(transaction.base_revision)?;
    if transaction.message.trim().is_empty()
        || transaction.message.chars().count() > 512
        || transaction.message.chars().any(char::is_control)
    {
        return Err(CoreError::new(
            "MESSAGE_INVALID",
            "Transaction message needs 1..512 non-control characters",
        ));
    }
    if transaction.commands.is_empty() || transaction.commands.len() > 1024 {
        return Err(CoreError::new(
            "TRANSACTION_LIMIT",
            "Transaction needs 1..1024 commands",
        ));
    }
    // Every command applies to private candidate state, never to the caller's snapshot.
    let mut candidate = project.clone();
    for command in &transaction.commands {
        match command {
            Command::AcceptanceSet { acceptance } => {
                if candidate.flange.is_some() {
                    return Err(CoreError::new(
                        "TYPE_MISMATCH",
                        "Legacy flange acceptance follows its parameter binding",
                    ));
                }
                candidate.acceptance = Some(acceptance.clone());
            }
            Command::ParameterSet { id, value } => {
                let parameter = candidate.parameters.get_mut(id).ok_or_else(|| {
                    CoreError::new("PARAMETER_NOT_FOUND", format!("Unknown parameter {id}"))
                })?;
                if parameter.value.unit() != value.unit() {
                    return Err(CoreError::new(
                        "TYPE_MISMATCH",
                        "A parameter's unit type cannot change in parameter.set",
                    ));
                }
                crate::validation::finite(value.number(), "parameter value")?;
                parameter.value = value.clone();
            }
            Command::ParameterCreate { parameter } => {
                if candidate.parameters.contains_key(&parameter.id) {
                    return Err(CoreError::new(
                        "ID_EXISTS",
                        "Parameter identity already exists",
                    ));
                }
                candidate.parameters.insert(parameter.id, parameter.clone());
            }
            Command::NodeRename { id, name } => {
                let node = candidate.nodes.get_mut(id).ok_or_else(|| {
                    CoreError::new("NODE_NOT_FOUND", format!("Unknown node {id}"))
                })?;
                node.name = name.clone();
            }
            Command::NodeReparent { id, parent } => {
                if *id == candidate.root_node_id {
                    return Err(CoreError::new(
                        "HIERARCHY_INVALID",
                        "Cannot reparent the project root",
                    ));
                }
                let node = candidate.nodes.get_mut(id).ok_or_else(|| {
                    CoreError::new("NODE_NOT_FOUND", format!("Unknown node {id}"))
                })?;
                node.parent = Some(*parent);
            }
            Command::FeatureCreate { feature } => {
                if candidate.features.contains_key(&feature.id) {
                    return Err(CoreError::new(
                        "ID_EXISTS",
                        "Feature identity already exists",
                    ));
                }
                candidate.features.insert(feature.id, feature.clone());
            }
            Command::FeatureReplace { feature } => {
                if !candidate.features.contains_key(&feature.id) {
                    return Err(CoreError::new(
                        "FEATURE_NOT_FOUND",
                        "Cannot replace a missing feature",
                    ));
                }
                candidate.features.insert(feature.id, feature.clone());
            }
            Command::FeatureDelete { id } => {
                if candidate.features.remove(id).is_none() {
                    return Err(CoreError::new(
                        "FEATURE_NOT_FOUND",
                        format!("Unknown feature {id}"),
                    ));
                }
            }
            Command::NodeCreate { node } => {
                if candidate.nodes.contains_key(&node.id) {
                    return Err(CoreError::new("ID_EXISTS", "Node identity already exists"));
                }
                candidate.nodes.insert(node.id, node.clone());
            }
            Command::NodeDelete { id } => {
                if *id == candidate.root_node_id {
                    return Err(CoreError::new(
                        "HIERARCHY_INVALID",
                        "Cannot delete the project root",
                    ));
                }
                if candidate.nodes.remove(id).is_none() {
                    return Err(CoreError::new(
                        "NODE_NOT_FOUND",
                        format!("Unknown node {id}"),
                    ));
                }
            }
        }
    }
    candidate.validate()?;
    if candidate == *project {
        return Err(CoreError::new(
            "EMPTY_CHANGE",
            "Transaction does not change engineering state",
        ));
    }
    Ok(candidate)
}

/// Conservative affected closure across the old and new dependency graphs.
/// Names and hierarchy alone do not change feature geometry. This is an
/// invalidation result, not a claim that every affected output changes bytes.
pub fn dirty_features(old: &Project, new: &Project) -> BTreeSet<Uuid> {
    if old.precision != new.precision {
        return old
            .features
            .keys()
            .chain(new.features.keys())
            .copied()
            .collect();
    }
    let changed_parameters: BTreeSet<_> = old
        .parameters
        .keys()
        .chain(new.parameters.keys())
        .copied()
        .filter(|id| {
            old.parameters.get(id).map(|p| &p.value) != new.parameters.get(id).map(|p| &p.value)
        })
        .collect();
    let mut dirty: BTreeSet<_> = old
        .features
        .keys()
        .chain(new.features.keys())
        .copied()
        .filter(|id| {
            old.features.get(id).map(|f| &f.operation) != new.features.get(id).map(|f| &f.operation)
        })
        .collect();
    let mut reverse: BTreeMap<Uuid, BTreeSet<Uuid>> = BTreeMap::new();
    for project in [old, new] {
        for (id, feature) in &project.features {
            if !feature
                .operation
                .parameter_ids()
                .is_disjoint(&changed_parameters)
            {
                dirty.insert(*id);
            }
            for input in feature.operation.dependencies() {
                reverse.entry(input).or_default().insert(*id);
            }
        }
    }
    let mut pending: Vec<_> = dirty.iter().copied().collect();
    while let Some(id) = pending.pop() {
        if let Some(dependents) = reverse.get(&id) {
            for dependent in dependents {
                if dirty.insert(*dependent) {
                    pending.push(*dependent);
                }
            }
        }
    }
    dirty
}
