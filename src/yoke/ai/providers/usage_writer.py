"""Transactional provider usage records, shared with SDK child processes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from yoke.session.repository.utils import default_database_path
from yoke.storage import open_database


class UsageLogWriteError(OSError):
    """Raised when a provider usage record cannot be persisted."""


def append_usage(
    record: dict[str, object], *, database_path: Path | None = None
) -> None:
    """Insert one privacy-safe metric atomically, retaining its reporting schema."""
    try:
        database = open_database(database_path or default_database_path())
        usage = record.get("usage")
        tokens = usage if isinstance(usage, dict) else {}
        with database.write() as connection:
            connection.execute(
                "INSERT INTO usage_events (event_id, completed_at, provider, model, "
                "surface, session_id, input_tokens, output_tokens, cached_tokens, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["event_id"],
                    record["completed_at"],
                    record["provider"],
                    record.get("model"),
                    record.get("surface"),
                    record.get("session_id"),
                    tokens.get("input_tokens"),
                    tokens.get("output_tokens"),
                    tokens.get("cached_input_tokens"),
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                ),
            )
    except (OSError, sqlite3.Error) as exc:
        raise UsageLogWriteError("Could not persist provider usage metric.") from exc


def read_usage_events(
    *,
    database_path: Path | None = None,
    provider: str | None = None,
    session_id: str | None = None,
    since: str | None = None,
) -> Iterator[dict[str, object]]:
    """Read reporting records in completion order from a consistent snapshot."""
    database = open_database(database_path or default_database_path())
    where = []
    parameters = []
    for column, value, operator in (
        ("provider", provider, "="),
        ("session_id", session_id, "="),
        ("completed_at", since, ">="),
    ):
        if value is not None:
            where.append(f"{column} {operator} ?")
            parameters.append(value)
    query = "SELECT data FROM usage_events"
    if where:
        query += " WHERE " + " AND ".join(where)
    with database.read() as connection:
        for (data,) in connection.execute(
            query + " ORDER BY completed_at, event_id", parameters
        ):
            yield json.loads(data)
