CREATE TABLE runs (
    run_id uuid PRIMARY KEY,
    spec jsonb NOT NULL,
    spec_sha256 text NOT NULL,
    state text NOT NULL CHECK (state IN ('running','paused','completed','failed','cancelled')),
    control_epoch bigint NOT NULL CHECK (control_epoch >= 0),
    event_seq bigint NOT NULL DEFAULT 0 CHECK (event_seq >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_dispatch_at timestamptz
);

CREATE TABLE tasks (
    run_id uuid NOT NULL REFERENCES runs(run_id),
    task_key text NOT NULL,
    spec jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending','running','retry_wait','succeeded','failed','cancelled')),
    execution_epoch bigint NOT NULL DEFAULT 0 CHECK (execution_epoch >= 0),
    current_attempt_id uuid,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    output jsonb,
    error text,
    PRIMARY KEY (run_id, task_key),
    CHECK ((state = 'succeeded') = (output IS NOT NULL))
);

CREATE TABLE dependencies (
    run_id uuid NOT NULL,
    task_key text NOT NULL,
    dependency_key text NOT NULL,
    PRIMARY KEY (run_id, task_key, dependency_key),
    FOREIGN KEY (run_id, task_key) REFERENCES tasks(run_id, task_key),
    FOREIGN KEY (run_id, dependency_key) REFERENCES tasks(run_id, task_key),
    CHECK (task_key <> dependency_key)
);

CREATE TABLE attempts (
    run_id uuid NOT NULL,
    task_key text NOT NULL,
    attempt_id uuid NOT NULL,
    worker_id text NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch > 0),
    control_epoch bigint NOT NULL CHECK (control_epoch >= 0),
    source jsonb NOT NULL,
    changes_mm jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('running','succeeded','failed','abandoned')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz NOT NULL,
    deadline timestamptz NOT NULL,
    finished_at timestamptz,
    error text,
    completion_digest text,
    failure_digest text,
    output jsonb,
    PRIMARY KEY (run_id, task_key, attempt_id),
    UNIQUE (run_id, task_key, execution_epoch),
    FOREIGN KEY (run_id, task_key) REFERENCES tasks(run_id, task_key),
    CHECK (lease_until <= deadline),
    CHECK ((state = 'succeeded') = (output IS NOT NULL)),
    CHECK ((completion_digest IS NOT NULL) = (output IS NOT NULL))
);

ALTER TABLE tasks ADD CONSTRAINT current_attempt_scope
    FOREIGN KEY (run_id, task_key, current_attempt_id)
    REFERENCES attempts(run_id, task_key, attempt_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE claim_receipts (
    worker_id text NOT NULL,
    claim_id uuid NOT NULL,
    request_digest text NOT NULL,
    run_id uuid,
    task_key text,
    attempt_id uuid,
    PRIMARY KEY (worker_id, claim_id),
    FOREIGN KEY (run_id, task_key, attempt_id) REFERENCES attempts(run_id, task_key, attempt_id),
    CHECK ((run_id IS NULL) = (task_key IS NULL)),
    CHECK ((run_id IS NULL) = (attempt_id IS NULL))
);

CREATE TABLE control_receipts (
    run_id uuid NOT NULL REFERENCES runs(run_id),
    operation_id uuid NOT NULL,
    request_digest text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (run_id, operation_id)
);

CREATE TABLE events (
    run_id uuid NOT NULL REFERENCES runs(run_id),
    seq bigint NOT NULL CHECK (seq > 0),
    event_id uuid NOT NULL UNIQUE,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX queue_runs ON runs (state, last_dispatch_at, created_at, run_id);
CREATE INDEX queue_tasks ON tasks (run_id, state, available_at, task_key);
CREATE INDEX expired_attempts ON attempts (run_id, lease_until) WHERE state = 'running';
