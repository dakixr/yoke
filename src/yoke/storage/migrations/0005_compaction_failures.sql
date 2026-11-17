-- Failure markers outlive restarts and are collected with their compaction row.
CREATE TABLE blob_compaction_failures (
    sha256 TEXT PRIMARY KEY REFERENCES blob_compaction(sha256) ON DELETE CASCADE,
    failures INTEGER NOT NULL CHECK(failures > 0)
);
