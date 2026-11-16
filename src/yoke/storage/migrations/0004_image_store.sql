-- Codecs are out of band: no user-supplied URL can impersonate a reference.
CREATE TABLE blob_codecs (
    sha256 TEXT PRIMARY KEY REFERENCES blobs(sha256) ON DELETE CASCADE,
    codec INTEGER NOT NULL CHECK(codec = 1),
    manifest TEXT NOT NULL,
    logical_size INTEGER NOT NULL
);
CREATE TABLE blob_refs (
    parent_sha TEXT NOT NULL REFERENCES blobs(sha256) ON DELETE CASCADE,
    child_sha TEXT NOT NULL REFERENCES blobs(sha256),
    PRIMARY KEY(parent_sha, child_sha)
) WITHOUT ROWID;
CREATE INDEX blob_refs_by_child ON blob_refs(child_sha);
CREATE TABLE blob_compaction (
    sha256 TEXT PRIMARY KEY REFERENCES blobs(sha256) ON DELETE CASCADE
);
ALTER TABLE entries ADD COLUMN metadata_sha256 TEXT REFERENCES blobs(sha256);
CREATE INDEX entries_by_metadata_blob ON entries(metadata_sha256);
