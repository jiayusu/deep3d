use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde::Serialize;
use uuid::Uuid;

use crate::model::Project;

/// Reverse Feature dependency index shared by edits and the G4 benchmark.
///
/// Edges point from an input Feature to the Feature that consumes it. The
/// index is the union of every graph it is built from, so edits that rewire or
/// delete Features still invalidate the old consumers.
#[derive(Debug, Clone, Default)]
pub struct DependencyIndex {
    slots: HashMap<Uuid, usize>,
    ids: Vec<Uuid>,
    consumers: Vec<Vec<usize>>,
}

/// Exact invalidated Feature set with the reason edges that justify it.
///
/// `reasons[child]` holds every invalidated direct input of `child`; seeds
/// carry no reason edge unless another invalidated Feature also feeds them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InvalidationPlan {
    pub seeds: BTreeSet<Uuid>,
    pub dirty: BTreeSet<Uuid>,
    pub reasons: BTreeMap<Uuid, BTreeSet<Uuid>>,
}

impl DependencyIndex {
    pub fn from_edges(edges: impl IntoIterator<Item = (Uuid, Uuid)>) -> Self {
        let mut index = Self::default();
        for (input, consumer) in edges {
            index.add_edge(input, consumer);
        }
        index.finish();
        index
    }

    pub fn from_projects(projects: &[&Project]) -> Self {
        let mut index = Self::default();
        for project in projects {
            for (id, feature) in &project.features {
                index.slot(*id);
                for input in feature.operation.dependencies() {
                    index.add_edge(input, *id);
                }
            }
        }
        index.finish();
        index
    }

    pub fn len(&self) -> usize {
        self.ids.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ids.is_empty()
    }

    fn slot(&mut self, id: Uuid) -> usize {
        *self.slots.entry(id).or_insert_with(|| {
            self.ids.push(id);
            self.consumers.push(Vec::new());
            self.ids.len() - 1
        })
    }

    fn add_edge(&mut self, input: Uuid, consumer: Uuid) {
        let input = self.slot(input);
        let consumer = self.slot(consumer);
        self.consumers[input].push(consumer);
    }

    fn finish(&mut self) {
        for consumers in &mut self.consumers {
            consumers.sort_unstable();
            consumers.dedup();
        }
    }

    /// Downstream closure of `seeds`. Unknown seeds are still invalidated:
    /// a Feature that exists in neither graph has no consumers to propagate to.
    pub fn invalidate(&self, seeds: &BTreeSet<Uuid>) -> InvalidationPlan {
        let mut marked = vec![false; self.ids.len()];
        let mut dirty = seeds.clone();
        let mut pending = Vec::new();
        for seed in seeds {
            if let Some(&slot) = self.slots.get(seed) {
                if !marked[slot] {
                    marked[slot] = true;
                    pending.push(slot);
                }
            }
        }
        let mut reasons: BTreeMap<Uuid, BTreeSet<Uuid>> = BTreeMap::new();
        while let Some(slot) = pending.pop() {
            let input = self.ids[slot];
            for &consumer in &self.consumers[slot] {
                reasons.entry(self.ids[consumer]).or_default().insert(input);
                if !marked[consumer] {
                    marked[consumer] = true;
                    dirty.insert(self.ids[consumer]);
                    pending.push(consumer);
                }
            }
        }
        InvalidationPlan {
            seeds: seeds.clone(),
            dirty,
            reasons,
        }
    }
}

/// Features whose own definition changed between two project states.
/// Names and hierarchy alone do not change Feature geometry.
pub fn changed_features(old: &Project, new: &Project) -> BTreeSet<Uuid> {
    let all = || old.features.keys().chain(new.features.keys()).copied();
    if old.precision != new.precision {
        return all().collect();
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
    all()
        .filter(|id| {
            let before = old.features.get(id);
            let after = new.features.get(id);
            before.map(|f| &f.operation) != after.map(|f| &f.operation)
                || [before, after].into_iter().flatten().any(|feature| {
                    !feature
                        .operation
                        .parameter_ids()
                        .is_disjoint(&changed_parameters)
                })
        })
        .collect()
}

/// Conservative affected closure across the old and new dependency graphs,
/// with the reason edges that explain every invalidated consumer.
pub fn invalidation_plan(old: &Project, new: &Project) -> InvalidationPlan {
    DependencyIndex::from_projects(&[old, new]).invalidate(&changed_features(old, new))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(n: u128) -> Uuid {
        Uuid::from_u128(n)
    }

    #[test]
    fn closure_records_every_dirty_input_as_a_reason() {
        // 1 -> 2 -> 4, 1 -> 3 -> 4, 5 -> 4
        let index = DependencyIndex::from_edges([
            (id(1), id(2)),
            (id(1), id(3)),
            (id(2), id(4)),
            (id(3), id(4)),
            (id(5), id(4)),
        ]);
        let plan = index.invalidate(&BTreeSet::from([id(1)]));
        assert_eq!(plan.dirty, BTreeSet::from([id(1), id(2), id(3), id(4)]));
        assert_eq!(plan.reasons[&id(4)], BTreeSet::from([id(2), id(3)]));
        assert_eq!(plan.reasons[&id(2)], BTreeSet::from([id(1)]));
        assert!(!plan.reasons.contains_key(&id(1)));
        let plan = index.invalidate(&BTreeSet::from([id(5)]));
        assert_eq!(plan.dirty, BTreeSet::from([id(4), id(5)]));
        assert_eq!(plan.reasons[&id(4)], BTreeSet::from([id(5)]));
    }

    #[test]
    fn unknown_or_empty_seeds_do_not_spread() {
        let index = DependencyIndex::from_edges([(id(1), id(2)), (id(1), id(2))]);
        assert_eq!(index.len(), 2);
        assert!(index.invalidate(&BTreeSet::new()).dirty.is_empty());
        let plan = index.invalidate(&BTreeSet::from([id(9)]));
        assert_eq!(plan.dirty, BTreeSet::from([id(9)]));
        assert!(plan.reasons.is_empty());
    }
}
