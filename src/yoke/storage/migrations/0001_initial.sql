-- Phase 1 storage core: sessions, conversation entries, durable events and inputs.

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'cli',
    workspace TEXT,
    title TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    provider TEXT,
    model TEXT,
    effort TEXT,
    context_window_tokens INTEGER,
    leaf_id TEXT,
    -- Bumped by entry appends, rewrites, leaf moves and entry metadata edits.
    tree_rev INTEGER NOT NULL DEFAULT 0,
    -- Bumped by every write to this row or its entries; read caches key on it.
    rev INTEGER NOT NULL DEFAULT 0,
    -- The rev of the last full entry rewrite; deltas cannot span it.
    rewrite_rev INTEGER NOT NULL DEFAULT 0,
    -- Last allocated entries.seq. Sequence numbers are never reused.
    entry_seq INTEGER NOT NULL DEFAULT 0,
    entry_count INTEGER NOT NULL DEFAULT 0,
    message_bytes INTEGER NOT NULL DEFAULT 0,
    context_usage TEXT,
    active_skills TEXT NOT NULL DEFAULT '[]',
    skill_dirs TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 5,
    created_at TEXT,
    updated_at TEXT,
    last_user_at TEXT
);

CREATE INDEX sessions_by_activity ON sessions (archived_at, updated_at);
CREATE INDEX sessions_by_workspace ON sessions (workspace, updated_at);

CREATE TABLE entries (
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    role TEXT,
    call_id TEXT,
    created_at TEXT,
    -- Exact bytes the entry codec writes for the whole ConversationEntry.
    -- Immutable after insert.
    message BLOB NOT NULL,
    -- NULL until an entry metadata edit replaces the metadata in message.
    metadata TEXT,
    -- sessions.rev at insert or at the last metadata edit.
    rev INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

CREATE UNIQUE INDEX entries_by_id ON entries (session_id, id);
CREATE INDEX entries_by_parent ON entries (session_id, parent_id);
CREATE INDEX entries_by_call ON entries (session_id, call_id);
CREATE INDEX entries_by_rev ON entries (session_id, rev);

CREATE TABLE events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    type TEXT NOT NULL,
    time TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    location TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

-- One row per input id. An admitted HTTP input carries its admission record;
-- a queued input (HTTP or CLI) carries a queue position and its pending payload.
CREATE TABLE inputs (
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    state TEXT,
    delivery TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    position INTEGER,
    prompt TEXT NOT NULL,
    admitted_seq INTEGER,
    promoted_seq INTEGER,
    admission TEXT,
    pending TEXT,
    PRIMARY KEY (session_id, id)
) WITHOUT ROWID;

CREATE INDEX inputs_by_position ON inputs (session_id, position);

-- Queue revision and CLI pending images. Separate from sessions because the
-- CLI can persist a queue before its first save creates the sessions row.
CREATE TABLE session_queues (
    session_id TEXT PRIMARY KEY,
    queue_rev INTEGER NOT NULL DEFAULT 0,
    pending_images TEXT NOT NULL DEFAULT '[]'
);
