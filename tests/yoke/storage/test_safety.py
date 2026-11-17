"""Storage failures never upgrade SDK state or destroy the migration source."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yoke.ai.providers.usage_writer import append_usage
from yoke.cli.db_app import db_app
from yoke.session import SessionStore
from yoke.storage.database import Database, migrations, schema_version


def old_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(migrations()[0][1])
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO sessions(id,title) VALUES ('saved','keep me')")


@pytest.mark.parametrize("version", [None, 0, 1, 99])
def test_sdk_usage_skips_incompatible_schema_without_modifying_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, version: int | None
) -> None:
    path = tmp_path / "sdk.db"
    if version == 1:
        old_database(path)
    elif version is not None:
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes() if path.exists() else None
    with caplog.at_level(logging.DEBUG):
        append_usage(
            {"event_id": "metric", "completed_at": "now", "provider": "demo"},
            database_path=path,
        )
    assert (path.read_bytes() if path.exists() else None) == before
    assert "Skipping provider usage" in caplog.text
    assert list(tmp_path.glob("*.pre-v*")) == []


def test_migration_backup_preserves_source_and_replaces_previous(
    tmp_path: Path,
) -> None:
    path = tmp_path / "yoke.db"
    old_database(path)
    previous = path.with_name("yoke.db.pre-v1")
    previous.write_bytes(b"previous backup")
    database = Database(path, migrate=True)
    database.close()
    backups = list(tmp_path.glob("yoke.db.pre-v*"))
    assert backups == [path.with_name(f"yoke.db.pre-v{schema_version()}")]
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT title FROM sessions").fetchone() == (
            "keep me",
        )
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_backup_failure_prevents_migration(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "yoke.db"
    old_database(path)
    before = path.read_bytes()
    # A directory at the backup's final path prevents atomic publication.
    path.with_name(f"yoke.db.pre-v{schema_version()}").mkdir()
    with pytest.raises((OSError, sqlite3.Error)):
        Database(path, migrate=True)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT title FROM sessions").fetchone() == (
            "keep me",
        )


def test_db_check_reports_codec_and_orphan_failures(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path, migrate=True)
    store.save("saved", [])
    monkeypatch.setenv("YOKE_DB_PATH", str(store.database_path))
    result = CliRunner().invoke(db_app, ["check"])
    assert result.exit_code == 0, result.output
    assert "quick_check: ok" in result.output
    assert "foreign keys: ok" in result.output
    with store.database.write() as connection:
        connection.execute("INSERT INTO blobs VALUES ('bad', X'00')")
        connection.execute("INSERT INTO blob_codecs VALUES ('bad', 1, '[]', 1)")
        connection.execute(
            "INSERT INTO events(session_id,seq,id,type,time,data) VALUES ('missing',1,'e','x','now','{}')"
        )
    result = CliRunner().invoke(db_app, ["check"])
    assert result.exit_code == 1
    assert "Corrupt entry blob" in result.output
    assert "orphan" in result.output


@pytest.mark.parametrize("kind", ["missing", "corrupt", "too-new", "old"])
def test_db_check_never_creates_or_migrates(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    path = tmp_path / "yoke.db"
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    elif kind == "old":
        old_database(path)
    elif kind == "too-new":
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=99")
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    result = CliRunner().invoke(db_app, ["check"])
    assert result.exit_code == 1
    assert str(path) in result.output
    assert "Traceback" not in result.output
    assert (path.read_bytes() if path.exists() else None) == before


def test_sqlite_full_rolls_back_append_and_retained_index(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from yoke.agent.models import ConversationEntry, Message
    from yoke.session import SessionTreeIndex

    store = SessionStore(tmp_path, migrate=True)
    record = store.save("s", [Message.user("saved")])
    index = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    before = record.model_copy(deep=True)
    entry = ConversationEntry(
        kind="assistant", message=Message.assistant("unsaved"), parent_id=record.leaf_id
    )
    write = store.database.write

    @contextmanager
    def full_on_commit():
        with write() as connection:
            yield connection
            error = sqlite3.OperationalError("database or disk is full")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error

    monkeypatch.setattr(store.database, "write", full_on_commit)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        store.save_tree_delta(
            "s",
            existing_record=record,
            tree_index=index,
            leaf_id=entry.id,
            appended_entries=[entry],
        )
    assert record == before
    assert index.leaf_id == before.leaf_id
    assert index.active_entries() == before.conversation_entries
    assert store.load("s") == before


def test_daemon_reports_database_startup_cause(tmp_path, monkeypatch, capsys):
    from yoke.daemon import server
    from yoke.daemon.paths import DaemonPaths

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not sqlite")
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    monkeypatch.setattr(server, "preload_runtime", lambda: None)
    monkeypatch.setattr(server, "_bind_unix", lambda _path: None)
    assert server.run_daemon(port=0, paths=DaemonPaths(tmp_path, tmp_path / "run")) == 1
    error = capsys.readouterr().err
    assert str(path) in error
    assert "not a database" in error
    assert "yoke db check" in error
    assert "pre-v" in error
    assert "Traceback" not in error


def test_spawning_clients_receive_database_startup_error(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from yoke.daemon import client
    from yoke.daemon.paths import DaemonPaths

    paths = DaemonPaths(tmp_path, tmp_path / "run")
    paths.ensure()
    message = f"Yoke database startup failed: {tmp_path / 'yoke.db'}: schema version 99. Upgrade Yoke."

    def spawn(_paths):
        paths.log.write_text(message + "\n")
        return SimpleNamespace(poll=lambda: 1)

    monkeypatch.setattr(client, "spawn_daemon", spawn)
    monkeypatch.setattr(client, "find_daemon", lambda _paths: None)
    with pytest.raises(client.DaemonUnavailable, match="schema version 99"):
        client.connect_or_spawn(paths, timeout=0.1)


def test_backup_disk_full_leaves_database_and_previous_backup_untouched(
    tmp_path, monkeypatch
):
    from yoke.storage import schema

    path = tmp_path / "yoke.db"
    old_database(path)
    before = path.read_bytes()
    previous = path.with_name("yoke.db.pre-v1")
    previous.write_bytes(b"previous safety net")
    connect = sqlite3.connect

    class FullConnection(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            raise sqlite3.OperationalError("database or disk is full")

    def full_source(*args, **kwargs):
        if isinstance(args[0], str) and "mode=ro" in args[0]:
            kwargs["factory"] = FullConnection
        return connect(*args, **kwargs)

    monkeypatch.setattr(schema.sqlite3, "connect", full_source)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        Database(path, migrate=True)
    assert path.read_bytes() == before
    assert previous.read_bytes() == b"previous safety net"
    assert list(tmp_path.glob("yoke.db.pre-v*")) == [previous]


def test_default_database_and_handoff_opens_cannot_migrate(tmp_path, monkeypatch):
    from yoke.session.handoff.builder import build_session_handoff
    from yoke.storage import DatabaseError, open_database

    path = tmp_path / "yoke.db"
    old_database(path)
    before = path.read_bytes()
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    for operation in [
        lambda: Database(path),
        lambda: open_database(path),
        lambda: build_session_handoff("saved"),
    ]:
        with pytest.raises(DatabaseError, match="schema version 1"):
            operation()
        assert path.read_bytes() == before


def test_spawn_does_not_report_an_old_database_failure(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from yoke.daemon import client
    from yoke.daemon.paths import DaemonPaths

    paths = DaemonPaths(tmp_path, tmp_path / "run")
    paths.ensure()
    paths.log.write_text("Yoke database startup failed: stale error from yesterday\n")
    os.utime(paths.log, ns=(1, 1))
    monkeypatch.setattr(
        client, "spawn_daemon", lambda _paths: SimpleNamespace(poll=lambda: 1)
    )
    monkeypatch.setattr(client, "find_daemon", lambda _paths: None)
    with pytest.raises(client.DaemonUnavailable, match="did not start"):
        client.connect_or_spawn(paths, timeout=0.01)
