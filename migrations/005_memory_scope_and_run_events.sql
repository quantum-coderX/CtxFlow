-- Project isolation and append-only run-event storage.
-- This migration is additive and safe to apply more than once.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS memories_project_validity_idx
    ON memories (project_id, validity, archived);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    event_type TEXT NOT NULL,
    event_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    previous_hash TEXT,
    event_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, event_hash)
);

CREATE INDEX IF NOT EXISTS run_events_project_created_idx
    ON run_events (project_id, created_at);
