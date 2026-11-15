"""Session id validation and schema policy."""

import re

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_RETENTION_DAYS = 30
SESSION_MAINTENANCE_INTERVAL_SECONDS = 5.0
CURRENT_SESSION_SCHEMA_VERSION = 5


def validate_session_id(session_id: str) -> str:
    """Return a session id after checking it is safe to use as a key or name."""
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "Session id must start with an alphanumeric character and "
            "use only letters, numbers, dot, underscore, or dash."
        )
    return session_id
