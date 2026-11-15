-- Store immutable entry bytes once, including across session forks.
CREATE TABLE blobs (
    sha256 TEXT PRIMARY KEY NOT NULL,
    data BLOB NOT NULL
);
-- A separate primary-key index keeps foreign-key checks on fork from reading
-- large blob records. WITHOUT ROWID would put data in that index's records.

INSERT OR IGNORE INTO blobs SELECT yoke_sha256(message), message FROM entries;

CREATE TABLE entries_v2 (
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    role TEXT,
    call_id TEXT,
    created_at TEXT,
    message_sha256 TEXT NOT NULL REFERENCES blobs (sha256),
    metadata TEXT,
    rev INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

INSERT INTO entries_v2 SELECT session_id, seq, id, parent_id, kind, role,
    call_id, created_at, yoke_sha256(message), metadata, rev FROM entries;
DROP TABLE entries;
ALTER TABLE entries_v2 RENAME TO entries;
CREATE UNIQUE INDEX entries_by_id ON entries (session_id, id);
CREATE INDEX entries_by_parent ON entries (session_id, parent_id);
CREATE INDEX entries_by_call ON entries (session_id, call_id);
CREATE INDEX entries_by_rev ON entries (session_id, rev);
CREATE INDEX entries_by_blob ON entries (message_sha256);

CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    -- Binding is an identity check, not a deletion cascade. Persisted native
    -- file references can outlive their original session, including in forks.
    session_id TEXT,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL REFERENCES blobs (sha256),
    expires_at TEXT NOT NULL,
    durable INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX uploads_by_session ON uploads (session_id);
CREATE INDEX uploads_by_expiry ON uploads (durable, expires_at);
CREATE INDEX uploads_by_blob ON uploads (sha256);

CREATE TABLE usage_events (
    event_id TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    surface TEXT,
    session_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    data TEXT NOT NULL
);
CREATE INDEX usage_by_time ON usage_events (completed_at);
CREATE INDEX usage_by_session ON usage_events (session_id, completed_at);
