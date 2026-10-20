"""SQLite schema for the Operator session registry (AFC-412 PR1).

One writer:  the registry service.  Sessions and alias history are immutable except for the
documented mutable columns;  every state change lands in one transaction together with its
events row and export-outbox rows, so a crash can never leak or double-issue a label.

Standard library only, Python 3.11+.
"""
from __future__ import annotations

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    record_id TEXT PRIMARY KEY,
    seat TEXT NOT NULL,
    platform TEXT NOT NULL,
    verified_host TEXT NOT NULL,
    profile_scope TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    thread_link TEXT NOT NULL DEFAULT '',
    apple_note_link TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    wake_capability TEXT NOT NULL DEFAULT 'unknown',
    registered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT,
    source TEXT NOT NULL,
    UNIQUE (platform, verified_host, profile_scope, native_session_id)
);

-- One allocation namespace per platform across all hosts (owner 2026-10-20, option B).
-- generation starts at 1;  a recycled letter gets max(prior generations) + 1.
CREATE TABLE IF NOT EXISTS alias_assignments (
    platform TEXT NOT NULL,
    letter TEXT NOT NULL,
    generation INTEGER NOT NULL,
    record_id TEXT NOT NULL REFERENCES sessions(record_id),
    assigned_at TEXT NOT NULL,
    retired_at TEXT,
    PRIMARY KEY (platform, letter, generation)
);
-- Active-assignment uniqueness:  at most one live holder of (platform, letter).
CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_active
    ON alias_assignments (platform, letter) WHERE retired_at IS NULL;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    session_record_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS export_outbox (
    outbox_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('session_upsert', 'event_append')),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    exported_at TEXT
);
"""
