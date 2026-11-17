"""Schema validation and serialized, backed-up upgrades for store owners."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from tempfile import NamedTemporaryFile


class DatabaseError(RuntimeError):
    """The Yoke database cannot be opened."""


class SchemaTooNewError(DatabaseError):
    """The database was migrated by a newer Yoke release."""

    def __init__(self, path: Path, found: int, supported: int) -> None:
        super().__init__(
            f"{path} uses schema version {found}, but this Yoke release supports "
            f"up to {supported}. Upgrade Yoke to open it."
        )
        self.found = found
        self.supported = supported


def migrations() -> list[tuple[int, str]]:
    """Return the ordered (version, sql) migrations shipped with this release."""
    folder = resources.files("yoke.storage").joinpath("migrations")
    found: list[tuple[int, str]] = []
    for item in folder.iterdir():
        name = item.name
        if not name.endswith(".sql"):
            continue
        found.append((int(name.split("_", 1)[0]), item.read_text(encoding="utf-8")))
    found.sort()
    for expected, (version, _) in enumerate(found, start=1):
        if version != expected:
            raise DatabaseError(f"Migration {expected:04d} is missing.")
    return found


def schema_version() -> int:
    """Return the newest schema version this release can migrate to."""
    return len(migrations())


def check_schema(connection: sqlite3.Connection, path: Path) -> None:
    """Require exactly this release's schema without creating or upgrading it."""
    found = connection.execute("PRAGMA user_version").fetchone()[0]
    supported = schema_version()
    if found > supported:
        raise SchemaTooNewError(path, found, supported)
    if found < supported:
        raise DatabaseError(
            f"{path} uses schema version {found}; this release needs {supported}. "
            "Open it with the Yoke daemon or an explicit yoke db command to migrate."
        )


def migrate_schema(connection: sqlite3.Connection, path: Path) -> None:
    """Back up a stable source before any migration SQL or journal change."""
    available = migrations()
    supported = len(available)
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > supported:
        raise SchemaTooNewError(path, current, supported)
    if (
        current == 0
        and not connection.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table')"
        ).fetchone()[0]
    ):
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    if current != supported:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current > supported:
                raise SchemaTooNewError(path, current, supported)
            populated = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table')"
            ).fetchone()[0]
            if current < supported and (current or populated):
                _backup_source(path, supported)
            for version, sql in available[current:]:
                for statement in _statements(sql):
                    connection.execute(statement)
                if version == 2:
                    from yoke.session.repository.provenance import (
                        classify_legacy_entries,
                    )

                    classify_legacy_entries(connection)
                connection.execute(f"PRAGMA user_version={version}")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise DatabaseError(f"Could not enable WAL for {path} ({mode}).")


def _backup_source(path: Path, target_version: int) -> None:
    target = path.with_name(f"{path.name}.pre-v{target_version}")
    # A separate reader can copy while BEGIN IMMEDIATE excludes other writers.
    # Never back up through the connection holding that write transaction.
    with NamedTemporaryFile(
        dir=path.parent, prefix=target.name + ".", delete=False
    ) as temporary:
        staging = Path(temporary.name)
    try:
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            destination = sqlite3.connect(staging)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
    # A failed backup keeps the previous safety net. Only completed backups
    # with the exact version suffix participate in retention.
    for previous in path.parent.glob(path.name + ".pre-v*"):
        suffix = previous.name.removeprefix(path.name + ".pre-v")
        if previous != target and suffix.isdecimal() and previous.is_file():
            previous.unlink()


def _statements(sql: str) -> Iterator[str]:
    buffer = ""
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise DatabaseError("Migration ends with an incomplete statement.")
