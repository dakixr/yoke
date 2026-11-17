-- Derived text only. Never index tool output, JSON, or image carriers.
CREATE VIRTUAL TABLE session_search USING fts5(
    session_id UNINDEXED, entry_id UNINDEXED, title, text,
    tokenize = 'unicode61'
);
