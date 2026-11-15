"""Session file names, validation, and schema policy."""

import re
from pathlib import Path

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_INDEX_NAME = "index.json"
SESSION_FILE_SUFFIX = ".jsonl"
SESSION_RETENTION_DAYS = 30
SESSION_MAINTENANCE_INTERVAL_SECONDS = 5.0
CURRENT_SESSION_SCHEMA_VERSION = 5


def session_path(directory: Path, session_id: str) -> Path:
    """Return the validated canonical path for a session."""
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "Session id must start with an alphanumeric character and "
            "use only letters, numbers, dot, underscore, or dash."
        )
    return directory / f"{session_id}{SESSION_FILE_SUFFIX}"
