CREATE TABLE projects (
    project_id uuid PRIMARY KEY,
    initial_source jsonb NOT NULL,
    goal_head uuid,
    goal_generation bigint NOT NULL DEFAULT 0 CHECK (goal_generation >= 0),
    public_head uuid,
    public_generation bigint NOT NULL DEFAULT 0 CHECK (public_generation >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((goal_head IS NULL) = (goal_generation = 0)),
    CHECK ((public_head IS NULL) = (public_generation = 0))
);

CREATE TABLE goal_revisions (
    project_id uuid NOT NULL REFERENCES projects(project_id),
    revision_id uuid NOT NULL,
    parent uuid,
    generation bigint NOT NULL CHECK (generation > 0),
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, revision_id),
    UNIQUE (project_id, generation),
    FOREIGN KEY (project_id,parent) REFERENCES goal_revisions(project_id,revision_id)
);

CREATE TABLE plan_revisions (
    project_id uuid NOT NULL REFERENCES projects(project_id),
    run_id uuid PRIMARY KEY REFERENCES runs(run_id),
    goal_revision_id uuid NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id,run_id),
    FOREIGN KEY (project_id,goal_revision_id) REFERENCES goal_revisions(project_id,revision_id)
);

CREATE TABLE verified_commits (
    project_id uuid NOT NULL REFERENCES projects(project_id),
    commit_id uuid NOT NULL,
    parent uuid,
    goal_revision_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_key text NOT NULL,
    attempt_id uuid NOT NULL,
    public_generation bigint NOT NULL CHECK (public_generation > 0),
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id,commit_id),
    UNIQUE (project_id,public_generation),
    UNIQUE (run_id,task_key,attempt_id),
    FOREIGN KEY (project_id,parent) REFERENCES verified_commits(project_id,commit_id),
    FOREIGN KEY (project_id,goal_revision_id) REFERENCES goal_revisions(project_id,revision_id),
    FOREIGN KEY (project_id,run_id) REFERENCES plan_revisions(project_id,run_id),
    FOREIGN KEY (run_id,task_key,attempt_id) REFERENCES attempts(run_id,task_key,attempt_id)
);

CREATE TABLE authority_receipts (
    project_id uuid NOT NULL REFERENCES projects(project_id),
    operation_id uuid NOT NULL,
    request_digest text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('register','goal')),
    result jsonb NOT NULL,
    PRIMARY KEY (project_id,operation_id)
);

ALTER TABLE projects ADD CONSTRAINT goal_head_scope FOREIGN KEY(project_id,goal_head)
    REFERENCES goal_revisions(project_id,revision_id);
ALTER TABLE projects ADD CONSTRAINT public_head_scope FOREIGN KEY(project_id,public_head)
    REFERENCES verified_commits(project_id,commit_id);

CREATE INDEX project_plans ON plan_revisions(project_id,run_id);
