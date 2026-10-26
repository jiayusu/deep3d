use super::*;

fn invalid_candidate(message: impl Into<String>) -> CoreError {
    error("CANDIDATE_INVALID", message)
}

impl ProjectStore {
    /// Create an unpublished one-transaction branch for independent evaluation.
    /// The source is read under its writer lock and is never modified.
    pub fn prepare_candidate(
        &self,
        transaction: &Transaction,
        path: impl AsRef<Path>,
    ) -> Result<Snapshot> {
        let path = normalized_path(path.as_ref(), false)?;
        if path == self.path {
            return Err(invalid_candidate("Candidate must have a different path"));
        }
        let (mut state, transaction_bytes, transaction_hash) = {
            let _source_lock = acquire_lock(&self.path)?;
            let state = load(&self.path)?;
            let transaction_bytes = canonical_bytes(transaction)?;
            let transaction_hash = digest_bytes(&transaction_bytes);
            if let Some(previous) = state
                .manifest
                .revisions
                .values()
                .find(|revision| revision.transaction_id == Some(transaction.id))
            {
                return Err(
                    if previous.transaction_hash.as_deref() != Some(&transaction_hash) {
                        error(
                            "IDEMPOTENCY_CONFLICT",
                            "Transaction identity was reused with different content",
                        )
                    } else {
                        error("REVISION_CONFLICT", "Transaction is already committed")
                    },
                );
            }
            if transaction.base_revision != state.manifest.head.revision_id
                || transaction.expected_generation != state.manifest.head.generation
            {
                return Err(error(
                    "REVISION_CONFLICT",
                    "Expected revision and generation no longer match branch head",
                ));
            }
            (state, transaction_bytes, transaction_hash)
        };
        let previous = snapshot(&state, transaction.base_revision)?;
        let project = apply_transaction(&previous.project, transaction)?;
        let spec_bytes = canonical_bytes(&project)?;
        let spec_hash = digest_bytes(&spec_bytes);
        let revision = Revision {
            id: Uuid::new_v4(),
            parent: Some(transaction.base_revision),
            spec_hash: spec_hash.clone(),
            transaction_id: Some(transaction.id),
            transaction_hash: Some(transaction_hash.clone()),
            message: transaction.message.clone(),
        };
        state.manifest.head = HeadToken {
            revision_id: revision.id,
            generation: transaction
                .expected_generation
                .checked_add(1)
                .ok_or_else(|| error("GENERATION_EXHAUSTED", "Branch generation cannot wrap"))?,
        };
        state
            .manifest
            .revisions
            .insert(revision.id, revision.clone());
        state.objects.insert(transaction_hash, transaction_bytes);
        state.objects.insert(spec_hash, spec_bytes);

        // Lock only the new destination while writing it. Geometry evaluation
        // runs later against this package without holding the source lock.
        let _candidate_lock = acquire_lock(&path)?;
        if path.exists() {
            return Err(error("PROJECT_EXISTS", "Candidate output already exists"));
        }
        save(&path, &state, true)?;
        snapshot(&state, revision.id)
    }

    /// Publish exactly one evaluated revision if its original branch token is
    /// still current. Metadata attached to the source after the fork is kept.
    pub fn adopt_candidate(
        &self,
        candidate: impl AsRef<Path>,
        expected: HeadToken,
        materialization: &str,
    ) -> Result<Snapshot> {
        let candidate_path = normalized_path(candidate.as_ref(), true)?;
        if candidate_path == self.path {
            return Err(invalid_candidate("Candidate must have a different path"));
        }
        let _source_lock = acquire_lock(&self.path)?;
        let mut source = load(&self.path)?;
        let candidate = load(&candidate_path)?;
        if source.manifest.project_id != candidate.manifest.project_id {
            return Err(invalid_candidate("Candidate belongs to another project"));
        }
        let candidate_head = snapshot(&candidate, candidate.manifest.head.revision_id)?;
        if candidate_head.revision.parent != Some(expected.revision_id)
            || expected.generation.checked_add(1) != Some(candidate_head.head.generation)
        {
            return Err(invalid_candidate(
                "Candidate must add exactly one revision to the expected branch token",
            ));
        }
        let candidate_history = ordered_revisions(&candidate)?;
        let source_history = ordered_revisions(&source)?;
        let prefix = &candidate_history[..candidate_history.len() - 1];
        if source_history.len() < prefix.len() || source_history[..prefix.len()] != *prefix {
            return Err(invalid_candidate(
                "Candidate history does not match the immutable source history",
            ));
        }
        if !candidate
            .manifest
            .materializations
            .contains(materialization)
        {
            return Err(error(
                "MATERIALIZATION_NOT_FOUND",
                "Selected materialization is missing from candidate",
            ));
        }
        let record: Materialization = canonical_decode(object(&candidate, materialization)?)?;
        if record.revision_id != candidate_head.revision.id {
            return Err(invalid_candidate(
                "Selected materialization is not bound to the candidate revision",
            ));
        }
        validate_materialization(&candidate, &record)?;

        // Hash-addressed objects and revision identities cannot change meaning
        // during a merge, even when the caller retries an accepted candidate.
        for (hash, bytes) in &candidate.objects {
            if source.objects.get(hash).is_some_and(|old| old != bytes) {
                return Err(invalid_candidate("Immutable content identity collision"));
            }
        }
        let revision = candidate_head.revision;
        if source
            .manifest
            .revisions
            .get(&revision.id)
            .is_some_and(|old| old != &revision)
        {
            return Err(invalid_candidate("Immutable revision identity collision"));
        }
        if let Some(previous) = source
            .manifest
            .revisions
            .values()
            .find(|old| old.transaction_id == revision.transaction_id)
        {
            if previous.transaction_hash != revision.transaction_hash {
                return Err(error(
                    "IDEMPOTENCY_CONFLICT",
                    "Transaction identity was reused with different content",
                ));
            }
            if previous.id != revision.id {
                return Err(error(
                    "REVISION_CONFLICT",
                    "Transaction was committed through a different revision",
                ));
            }
        }
        if source.manifest.revisions.get(&revision.id) == Some(&revision)
            && source.manifest.materializations.contains(materialization)
        {
            return snapshot(&source, revision.id);
        }
        if source.manifest.head != expected {
            return Err(error(
                "REVISION_CONFLICT",
                "Expected revision and generation no longer match branch head",
            ));
        }

        source
            .manifest
            .revisions
            .insert(revision.id, revision.clone());
        source.manifest.head = candidate.manifest.head;
        source
            .manifest
            .materializations
            .extend(candidate.manifest.materializations);
        source.objects.extend(candidate.objects);
        save(&self.path, &source, false)?;
        snapshot(&source, revision.id)
    }
}
