CREATE TABLE IF NOT EXISTS pipeline_runs (        -- Holds Operational Logs
    run_id              TEXT PRIMARY KEY,
    org                 TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    records_fetched     INTEGER NOT NULL DEFAULT 0,
    records_valid       INTEGER NOT NULL DEFAULT 0,
    records_quarantined INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running', 'success', 'failed'))
);


CREATE UNIQUE INDEX IF NOT EXISTS one_running_run_per_org
    ON pipeline_runs (org)              --sends a 409 if a second run is started for the same org while the first is still running
    WHERE status = 'running';

CREATE TABLE IF NOT EXISTS github_repos (
    id                 BIGINT PRIMARY KEY,
    organization       TEXT NOT NULL,
    name               TEXT NOT NULL,
    full_name          TEXT NOT NULL,
    private            BOOLEAN NOT NULL,
    html_url           TEXT NOT NULL,
    description        TEXT,
    stargazers_count   INTEGER NOT NULL CHECK (stargazers_count >= 0),
    forks_count        INTEGER NOT NULL CHECK (forks_count >= 0),
    open_issues_count  INTEGER NOT NULL CHECK (open_issues_count >= 0),
    archived           BOOLEAN NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    pushed_at          TIMESTAMPTZ NOT NULL,
    flags              TEXT[] NOT NULL DEFAULT '{}',
    last_run_id        TEXT REFERENCES pipeline_runs (run_id),
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Mirrors the cross-field check already enforced in src/schema.py's count it as 
    -- the second checkpoint in the database layer to ensure data integrity.

    CHECK (pushed_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_github_repos_organization
    ON github_repos (organization);



CREATE TABLE IF NOT EXISTS github_repos_quarantine (
    quarantine_id   BIGSERIAL PRIMARY KEY,


    dedup_key       TEXT NOT NULL UNIQUE,

    run_id          TEXT NOT NULL REFERENCES pipeline_runs (run_id),
    organization    TEXT NOT NULL,
    raw_payload     JSONB NOT NULL,
    failed_field    TEXT NOT NULL,
    error_message   TEXT NOT NULL,
    quarantined_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_quarantine_organization
    ON github_repos_quarantine (organization);