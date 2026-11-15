"""Inspection and backup for the Yoke database (`yoke db info|backup`)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from yoke.storage.database import BUSY_TIMEOUT_MS, Database, schema_version


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    """Sizes and counts reported by `yoke db info`."""

    path: Path
    schema_version: int
    supported_schema_version: int
    file_bytes: int
    wal_bytes: int
    page_size: int
    page_count: int
    freelist_count: int
    sessions: int
    entries: int
    events: int
    inputs: int

    def lines(self) -> list[str]:
        """Return human-readable report lines."""
        return [
            f"Path: {self.path}",
            f"Schema version: {self.schema_version} "
            f"(this release supports {self.supported_schema_version})",
            f"Database size: {_bytes(self.file_bytes)}",
            f"WAL size: {_bytes(self.wal_bytes)}",
            f"Pages: {self.page_count} x {self.page_size} B "
            f"({self.freelist_count} free)",
            f"Sessions: {self.sessions}",
            f"Entries: {self.entries}",
            f"Events: {self.events}",
            f"Inputs: {self.inputs}",
        ]


def database_info(database: Database) -> DatabaseInfo:
    """Collect sizes and row counts from one consistent snapshot."""
    with database.read() as connection:

        def scalar(sql: str) -> int:
            return int(connection.execute(sql).fetchone()[0])

        version = scalar("PRAGMA user_version")
        page_size = scalar("PRAGMA page_size")
        page_count = scalar("PRAGMA page_count")
        freelist = scalar("PRAGMA freelist_count")
        sessions = scalar("SELECT count(*) FROM sessions")
        entries = scalar("SELECT coalesce(sum(entry_count), 0) FROM sessions")
        events = scalar("SELECT count(*) FROM events")
        inputs = scalar("SELECT count(*) FROM inputs")
    return DatabaseInfo(
        path=database.path,
        schema_version=version,
        supported_schema_version=schema_version(),
        file_bytes=_size(database.path),
        wal_bytes=_size(database.path.with_name(database.path.name + "-wal")),
        page_size=page_size,
        page_count=page_count,
        freelist_count=freelist,
        sessions=sessions,
        entries=entries,
        events=events,
        inputs=inputs,
    )


def backup_database(database: Database, target: Path) -> Path:
    """Write a compact, consistent copy with ``VACUUM INTO``."""
    target = target.expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"Backup target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # VACUUM cannot run inside a transaction, so use a private connection.
    connection = sqlite3.connect(database.path, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()
    return target


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{value} B"
        size /= 1024
    return f"{value} B"
