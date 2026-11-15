"""One SQLite database shared by every Yoke process through WAL.

Each process owns one writer connection per database file. Writers serialize
on a re-entrant lock and run ``BEGIN IMMEDIATE`` transactions, so the CLI and
the HTTP daemon can both write and wait for each other through
``busy_timeout``. A nested ``write()`` on the owning thread joins the open
transaction through a savepoint. Readers use one connection per thread and see
the last committed snapshot; a thread that holds the write transaction reads
through the writer so it sees its own uncommitted rows.
"""

from __future__ import annotations

import os
import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from weakref import WeakValueDictionary

BUSY_TIMEOUT_MS = 5_000


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


class Database:
    """Connections and transactions for one database file in this process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0
        self._callbacks: list[tuple[int, Callable[[], None]]] = []
        self._local = threading.local()
        self._pid = os.getpid()
        self._writer = self._connect()
        try:
            # Conversion of an existing large v1 database can exceed the
            # normal write timeout. Concurrent starters wait for its migration.
            self._writer.execute("PRAGMA busy_timeout=60000")
            self._migrate()
            self._writer.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except BaseException:
            self._writer.close()
            raise
        self._identity = _file_identity(path)

    def _connect(self, *, query_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.create_function(
            "yoke_sha256",
            1,
            lambda data: hashlib.sha256(data).hexdigest(),
            deterministic=True,
        )
        connection.create_function("yoke_casefold", 1, str.casefold, deterministic=True)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        if query_only:
            connection.execute("PRAGMA query_only=ON")
        return connection

    def _migrate(self) -> None:
        available = migrations()
        supported = len(available)
        connection = self._writer
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current > supported:
            raise SchemaTooNewError(self.path, current, supported)
        if current == 0:
            # Only effective before the first table exists.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise DatabaseError(f"Could not enable WAL for {self.path} ({mode}).")
        if current == supported:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have migrated while this one waited.
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current > supported:
                raise SchemaTooNewError(self.path, current, supported)
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

    def maintain(self, *, pages: int = 256) -> None:
        """Collect unreferenced blobs and reclaim a bounded number of idle pages.

        Called by periodic session maintenance, outside turn transactions.
        Existing databases without auto_vacuum need an offline VACUUM first.
        """
        # Maintenance never queues behind a local or cross-process writer.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._depth:
                return
            self._writer.execute("PRAGMA busy_timeout=0")
            try:
                with self.write() as connection:
                    connection.execute(
                        "DELETE FROM blobs WHERE sha256 IN (SELECT sha256 FROM blobs "
                        "WHERE NOT EXISTS (SELECT 1 FROM entries WHERE message_sha256 = blobs.sha256) "
                        "AND NOT EXISTS (SELECT 1 FROM uploads WHERE sha256 = blobs.sha256) LIMIT 128)"
                    )
                    for _ in range(max(0, pages)):
                        if (
                            connection.execute("PRAGMA freelist_count").fetchone()[0]
                            == 0
                        ):
                            break
                        connection.execute("PRAGMA incremental_vacuum(1)").fetchall()
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise
            finally:
                self._writer.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        finally:
            self._lock.release()

    def is_current(self) -> bool:
        """Return whether this object still owns the file now at its path."""
        return self._pid == os.getpid() and self._identity == _file_identity(self.path)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run one serialized write transaction, joining an open one on this thread."""
        self._check_process()
        with self._lock:
            if self._depth:
                with self._savepoint():
                    yield self._writer
                return
            connection = self._writer
            connection.execute("BEGIN IMMEDIATE")
            self._owner = threading.get_ident()
            self._depth = 1
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                self._callbacks.clear()
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0
                self._owner = None
            callbacks = [callback for _, callback in self._callbacks]
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    @contextmanager
    def _savepoint(self) -> Iterator[None]:
        name = f"yoke_{self._depth}"
        level = self._depth
        self._writer.execute(f"SAVEPOINT {name}")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._writer.execute(f"ROLLBACK TO {name}")
            self._writer.execute(f"RELEASE {name}")
            self._callbacks = [item for item in self._callbacks if item[0] < level]
            raise
        else:
            self._writer.execute(f"RELEASE {name}")
        finally:
            self._depth = level

    def in_write(self) -> bool:
        """Return whether the current thread holds the write transaction."""
        return self._owner == threading.get_ident()

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run a callback once the current write commits, or now outside one."""
        if self.in_write():
            self._callbacks.append((self._depth - 1, callback))
            return
        callback()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Read one consistent committed snapshot (or this thread's open write)."""
        if self.in_write():
            yield self._writer
            return
        self._check_process()
        connection = self._reader()
        if connection.in_transaction:
            yield connection
            return
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.execute("COMMIT")

    def _reader(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect(query_only=True)
            self._local.connection = connection
        return connection

    def _check_process(self) -> None:
        if self._pid != os.getpid():
            raise DatabaseError("A database connection cannot cross a fork.")

    def close(self) -> None:
        """Close the writer and this thread's reader."""
        with self._lock:
            self._writer.close()
        reader = getattr(self._local, "connection", None)
        if reader is not None:
            reader.close()
            self._local.connection = None


_databases: WeakValueDictionary[Path, Database] = WeakValueDictionary()
_databases_lock = threading.Lock()


def open_database(path: Path) -> Database:
    """Return this process's shared database for a file, opening it on first use."""
    key = Path(os.path.abspath(path.expanduser()))
    with _databases_lock:
        database = _databases.get(key)
        if database is not None and database.is_current():
            return database
        database = Database(key)
        _databases[key] = database
        return database


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


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
