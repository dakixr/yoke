-- Preserve imported history until its next conversation save or tree edit.
-- This does not change user-visible timestamps or pin state.
ALTER TABLE sessions ADD COLUMN import_hold INTEGER NOT NULL DEFAULT 0;
