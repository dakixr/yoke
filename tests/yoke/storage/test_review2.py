"""Regression coverage for corrupt children and explicit schema ownership."""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from tests.yoke.storage.test_image_codec import image_url, quiet
from tests.yoke.storage.test_safety import old_database
from yoke.agent.models import Message
from yoke.cli.db_app import db_app
from yoke.session import SessionStore
from yoke.storage import DatabaseError
from yoke.storage.compactor import compact_batch
from yoke.storage.database import schema_version


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("first_pass", ["batch", "cli"])
def test_corrupt_child_isolated_from_compaction_and_other_reads(
    tmp_path, monkeypatch, damage, first_pass
):
    store = SessionStore(tmp_path, migrate=True)
    saved = store.save(
        "broken",
        [
            Message.user([{"type": "image_url", "image_url": {"url": image_url()}}]),
            Message.assistant("still readable"),
        ],
    )
    quiet(store.database)
    with store.database.write() as connection:
        parent, child = connection.execute(
            "SELECT parent_sha, child_sha FROM blob_refs"
        ).fetchone()
        if damage == "corrupt":
            connection.execute("UPDATE blobs SET data=X'00' WHERE sha256=?", (child,))
        else:
            # Keep the manifest pointing at a missing child, as after disk damage.
            connection.execute("DELETE FROM blob_refs WHERE child_sha=?", (child,))
            connection.execute("DELETE FROM blobs WHERE sha256=?", (child,))
    monkeypatch.setenv("YOKE_DB_PATH", str(store.database_path))
    runner = CliRunner()
    if first_pass == "batch":
        result = compact_batch(store.database)
        assert result.failures_inline == 1
    else:
        result = runner.invoke(db_app, ["compact"])
        assert result.exit_code == 0, result.output
        assert "failures_inline: 1" in result.output
    store.database.close()
    store = SessionStore(tmp_path)
    assert compact_batch(store.database).processed == 0
    with store.database.read() as connection:
        assert connection.execute(
            "SELECT failures FROM blob_compaction_failures WHERE sha256=?", (parent,)
        ).fetchone() == (1,)
    assert [entry.id for entry in store.list_index_entries()] == ["broken"]
    good = saved.conversation_entries[1]
    assert store.read_entries("broken", [good.id]) == [good]
    with pytest.raises(ValueError, match="broken.*Missing or corrupt image blob"):
        store.read_entries("broken", [saved.conversation_entries[0].id])
    checked = runner.invoke(db_app, ["check"])
    assert checked.exit_code == 1
    assert child in checked.output
    for _ in range(2):
        compacted = runner.invoke(db_app, ["compact"])
        assert compacted.exit_code == 0, compacted.output
        assert "processed: 0" in compacted.output
    stats = runner.invoke(db_app, ["stats"])
    assert stats.exit_code == 0, stats.output
    assert "Unreadable entry" in stats.output
    assert "assistant: 1," in stats.output


@pytest.mark.parametrize("command", ["info", "stats", "check"])
def test_read_commands_do_not_migrate_old_schema(tmp_path, monkeypatch, command):
    path = tmp_path / "old.db"
    old_database(path)
    before = path.read_bytes()
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    result = CliRunner().invoke(db_app, [command])
    assert result.exit_code == 1
    assert "yoke db migrate" in result.output
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.pre-v*")) == []


def test_store_defaults_to_schema_check(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)
    with pytest.raises(DatabaseError, match="yoke db migrate"):
        SessionStore(tmp_path, database_path=path)


def test_explicit_migrate_excludes_live_daemon(tmp_path, monkeypatch):
    from yoke.daemon.instance import InstanceLock
    from yoke.daemon.paths import DaemonPaths

    path = tmp_path / "old.db"
    old_database(path)
    before = path.read_bytes()
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    lock = InstanceLock.acquire(DaemonPaths.default())
    assert lock is not None
    try:
        refused = CliRunner().invoke(db_app, ["migrate"])
        assert refused.exit_code == 1
        assert "daemon" in refused.output.lower()
        assert path.read_bytes() == before
    finally:
        lock.release()
    migrated = CliRunner().invoke(db_app, ["migrate"])
    assert migrated.exit_code == 0, migrated.output
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            schema_version(),
        )
        assert connection.execute("SELECT title FROM sessions").fetchone() == (
            "keep me",
        )
    assert CliRunner().invoke(db_app, ["migrate"]).exit_code == 0


def test_explicit_migrate_keeps_lifetime_lock_through_backup(tmp_path, monkeypatch):
    from yoke.daemon.instance import InstanceLock, daemon_alive
    from yoke.daemon.paths import DaemonPaths
    from yoke.storage import schema

    path = tmp_path / "old.db"
    old_database(path)
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    backup = schema._backup_source
    observed = []

    def while_locked(*args):
        paths = DaemonPaths.default()
        observed.append(daemon_alive(paths))
        assert InstanceLock.acquire(paths, wait_seconds=0) is None
        backup(*args)

    monkeypatch.setattr(schema, "_backup_source", while_locked)
    result = CliRunner().invoke(db_app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert observed == [True]
    assert not daemon_alive(DaemonPaths.default())


def test_idle_worker_timeout_is_deferral_not_permanent_failure(tmp_path, monkeypatch):
    import subprocess

    from tests.yoke.storage.test_image_codec import legacy
    from yoke.storage import image_worker

    store = SessionStore(tmp_path, migrate=True)
    store.save(
        "timeout",
        [Message.user([{"type": "image_url", "image_url": {"url": image_url()}}])],
    )
    legacy(store.database)
    quiet(store.database)
    timeouts = []

    def expired(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with monkeypatch.context() as patch:
        patch.setattr(image_worker.subprocess, "run", expired)
        result = compact_batch(store.database, nonblocking=True)
    assert result.processed == result.failures_inline == 0
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.2
    assert compact_batch(store.database).processed == 1


def test_idle_image_budget_defers_without_poisoning(tmp_path, monkeypatch):
    from tests.yoke.storage.test_image_codec import legacy
    from yoke.storage import compactor, entry_transform

    store = SessionStore(tmp_path, migrate=True)
    urls = [image_url(color=color) for color in ("red", "green", "blue")]
    saved = store.save(
        "many",
        [
            Message.user(
                [{"type": "image_url", "image_url": {"url": url}} for url in urls]
            )
        ],
    )
    legacy(store.database)
    quiet(store.database)
    now = [0.0]
    calls = []

    def image_work(raw, **kwargs):
        calls.append(kwargs)
        now[0] += 0.21
        from yoke.agent.image_data import image_bytes_to_data_url

        return image_bytes_to_data_url(raw)

    with monkeypatch.context() as patch:
        patch.setattr(compactor.time, "monotonic", lambda: now[0])
        patch.setattr(entry_transform, "reencode_image", image_work)
        result = compact_batch(store.database, nonblocking=True)
        assert len(calls) == 1
        assert 0 < calls[0]["timeout"] <= 0.2
        assert result.processed == result.failures_inline == 0
    assert store.load("many").messages == saved.messages
    with store.database.read() as connection:
        assert connection.execute(
            "SELECT count(*) FROM blob_compaction_failures"
        ).fetchone() == (0,)
    # The explicit offline path can finish a candidate that exceeds idle budget.
    assert compact_batch(store.database).processed == 1


def test_provenance_skips_a_corrupt_chain_and_classifies_other_entries(tmp_path):
    from yoke.agent.models import ConversationEntry, ToolCall, ToolFunction
    from yoke.session import SessionRecord
    from yoke.session.repository.provenance import classify_legacy_entries

    store = SessionStore(tmp_path, migrate=True)
    for owner, color in (("broken", "red"), ("healthy", "blue")):
        call = ConversationEntry(
            id="call",
            kind="assistant_tool_calls",
            message=Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="attach",
                        function=ToolFunction(name="attach_image", arguments="{}"),
                    )
                ],
            ),
        )
        result = ConversationEntry(
            id="result",
            parent_id="call",
            kind="tool_result",
            message=Message(
                role="tool",
                tool_call_id="attach",
                content="attached",
            ),
        )
        image = ConversationEntry(
            id="image",
            parent_id="result",
            kind="user",
            message=Message.user(
                [{"type": "image_url", "image_url": {"url": image_url(color=color)}}]
            ),
        )
        assert store.insert_record(
            SessionRecord(
                id=owner, conversation_entries=[call, result, image], leaf_id="image"
            )
        )
    with store.database.write() as connection:
        child = connection.execute(
            "SELECT r.child_sha FROM blob_refs r JOIN entries e ON e.message_sha256=r.parent_sha WHERE e.session_id='broken'"
        ).fetchone()[0]
        connection.execute("UPDATE blobs SET data=X'00' WHERE sha256=?", (child,))
        connection.execute("UPDATE entries SET kind='user' WHERE id='image'")
        classify_legacy_entries(connection)
        assert dict(
            connection.execute("SELECT session_id, kind FROM entries WHERE id='image'")
        ) == {
            "broken": "user",
            "healthy": "tool_context",
        }
