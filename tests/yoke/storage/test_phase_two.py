"""V2 byte preservation, reference ownership, bounded reads and WAL writers."""

from __future__ import annotations

# ruff: noqa: S101,S603

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from yoke.agent.models import Message
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.repository.rows import encode_entry
from yoke.storage.database import Database, migrations


def test_v1_migration_preserves_raw_entry_bytes_and_mutable_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.db"
    # A real v1 schema, including a noncanonical JSON encoding. Migration must
    # hash the original bytes, never decode and re-encode them.
    source = SessionStore(tmp_path / "source")
    record = source.save(
        "original", [Message.user("Unicode é  \n"), Message.assistant("ok")]
    )
    raws = [
        json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, indent=2).encode()
        for entry in record.conversation_entries
    ]
    with sqlite3.connect(path) as connection:
        connection.executescript(migrations()[0][1])
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "INSERT INTO sessions (id, leaf_id, entry_count, message_bytes) VALUES (?, ?, ?, ?)",
            (record.id, record.leaf_id, len(raws), sum(map(len, raws))),
        )
        for seq, (entry, raw) in enumerate(zip(record.conversation_entries, raws), 1):
            connection.execute(
                "INSERT INTO entries (session_id, seq, id, parent_id, kind, message, metadata, rev) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    record.id,
                    seq,
                    entry.id,
                    entry.parent_id,
                    entry.kind,
                    raw,
                    '{"label":"edited"}' if seq == 1 else None,
                ),
            )
    migrated = Database(path)
    with migrated.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = connection.execute(
            "SELECT blobs.data, message_sha256, metadata FROM entries JOIN blobs ON blobs.sha256 = message_sha256 ORDER BY seq"
        ).fetchall()
        assert [row[0] for row in rows] == raws
        assert [row[1] for row in rows] == [
            hashlib.sha256(raw).hexdigest() for raw in raws
        ]
        assert rows[0][2] == '{"label":"edited"}'
        assert "message" not in {
            row[1] for row in connection.execute("PRAGMA table_info(entries)")
        }
    migrated.close()
    # Reopening is idempotent; no second conversion or record changes.
    store = SessionStore(database_path=path)
    loaded = store.load(record.id)
    assert loaded.conversation_entries[0].metadata == {"label": "edited"}
    assert loaded.messages[0].display_text_content() == "Unicode é  \n"


def test_fork_shares_blobs_and_gc_keeps_surviving_references(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.save(
        "original", [Message.user("payload" * 1000), Message.assistant("ok")]
    )
    expected = [encode_entry(entry) for entry in record.conversation_entries]
    store.fork("original", new_session_id_value="fork", materialize_result=False)
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 4
    store.delete("original")
    store.database.maintain()
    assert [encode_entry(entry) for entry in store.iter_entries("fork")] == expected
    store.delete("fork")
    store.database.maintain()
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_sql_pages_fetch_only_selected_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    store.save("large", [Message.user(str(n)) for n in range(150)])
    selected: list[str] = []
    original_read = store.read_entries

    def read(session_id, ids, **kwargs):  # noqa: ANN001,ANN202,ANN003
        selected.extend(ids)
        return original_read(session_id, ids, **kwargs)

    monkeypatch.setattr(store, "read_entries", read)
    monkeypatch.setattr(
        store,
        "entry_topology",
        lambda *_: pytest.fail("pagination must not load full topology"),
    )
    monkeypatch.setattr(
        store, "load", lambda *_: pytest.fail("pagination must not load full records")
    )
    queries = SessionMessageIndex(store)
    page = queries.page("large", limit=7, order="desc", anchor_id=None)
    assert page is not None and page.has_more
    assert len(selected) == 7
    assert [
        entry.message.display_text_content() for entry in page.entries if entry.message
    ] == [str(n) for n in range(149, 142, -1)]
    selected.clear()
    older = queries.page("large", limit=7, order="desc", anchor_id=page.entries[-1].id)
    assert older is not None and older.has_more
    assert len(selected) == 7
    assert not set(entry.id for entry in page.entries) & set(selected)


def test_sdk_usage_process_writes_during_session_transactions(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    store = SessionStore(database_path=path)
    store.save("shared", [Message.user("initial")])
    code = """
import os
from yoke.ai.providers.usage_writer import append_usage
print('ready', flush=True)
input()
for n in range(40):
    append_usage(dict(event_id=str(n), completed_at='2026-11-15', provider='sdk', surface='sdk', usage=dict(input_tokens=n)))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "YOKE_DB_PATH": str(path),
            "PYTHONPATH": os.pathsep.join(sys.path),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert (
            process.stdout is not None and process.stdout.readline().strip() == "ready"
        )
        assert process.stdin is not None
        with store.database.write():
            process.stdin.write("go\n")
            process.stdin.flush()
            store.set_title("shared", "concurrent")
        for n in range(40):
            store.set_title(
                "shared", str(n), existing_record=store.summary_record("shared")
            )
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        with store.database.read() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute(
                "SELECT count(*), sum(input_tokens) FROM usage_events"
            ).fetchone() == (40, 780)
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.load("shared").title == "39"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_upload_bytes_survive_removed_materialization_and_dedupe(
    tmp_path: Path,
) -> None:
    import shutil

    store = SessionStore(tmp_path)
    store.save("owner", [Message.user("hello")])
    service = UploadService(store)
    first = asyncio.run(
        service.create(
            UploadFile(filename="one.txt", file=BytesIO(b"same bytes")), session_id=None
        )
    )
    second = asyncio.run(
        service.create(
            UploadFile(filename="two.txt", file=BytesIO(b"same bytes")), session_id=None
        )
    )
    service.pin(first.uri, session_id="owner", name=first.name, mime=first.mime)
    path = service.resolve(
        first.uri, session_id="owner", name=first.name, mime=first.mime
    )
    shutil.rmtree(service.directory)
    restarted = UploadService(SessionStore(tmp_path))
    assert path.read_bytes() == b"same bytes"
    with store.database.write() as connection:
        assert (
            connection.execute("SELECT count(DISTINCT sha256) FROM uploads").fetchone()[
                0
            ]
            == 1
        )
        connection.execute(
            "UPDATE uploads SET expires_at = ?",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
        )
    assert restarted.gc_expired_orphans() == 1
    assert (
        restarted.resolve(
            first.uri, session_id="owner", name=first.name, mime=first.mime
        ).read_bytes()
        == b"same bytes"
    )
    assert not restarted.delete_bound_upload(second.uri, session_id="owner")
    store.delete("owner")
    store.database.maintain()
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM uploads").fetchone()[0] == 1
    assert (
        restarted.resolve(
            first.uri, session_id="owner", name=first.name, mime=first.mime
        ).read_bytes()
        == b"same bytes"
    )
    assert restarted.delete_bound_upload(first.uri, session_id="owner")
    store.database.maintain()
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


def test_incremental_vacuum_reclaims_only_the_idle_page_budget(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save("large", [Message.user("x" * 1_000_000)])
    store.delete("large")
    store.database.maintain(pages=0)
    with store.database.read() as connection:
        before = connection.execute("PRAGMA freelist_count").fetchone()[0]
    assert before > 10
    store.database.maintain(pages=4)
    with store.database.read() as connection:
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == before - 4


def test_phase_one_upload_import_preserves_bytes_and_binding(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save("owner", [Message.user("hello")])
    upload_id = "upl_" + "a" * 24
    directory = tmp_path / "uploads" / upload_id
    path = directory / "files" / "legacy.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"phase one upload")
    metadata = directory / "metadata.json"
    metadata.write_text(
        json.dumps(
            dict(
                id=upload_id,
                session_id="owner",
                name="legacy.txt",
                mime="text/plain",
                size=16,
                path=str(path),
                expires_at="2000-01-01",
                durable=True,
            )
        )
    )
    service = UploadService(store)
    assert not metadata.exists()
    assert (
        service.resolve(
            f"yoke-upload://{upload_id}",
            session_id="owner",
            name="legacy.txt",
            mime="text/plain",
        )
        == path
    )
    assert path.read_bytes() == b"phase one upload"
    assert service.gc_expired_orphans() == 0
    with store.database.read() as connection:
        assert connection.execute(
            "SELECT session_id, durable FROM uploads"
        ).fetchone() == ("owner", 1)


def test_idle_maintenance_skips_another_process_writer(tmp_path: Path) -> None:
    import time

    store = SessionStore(tmp_path)
    with sqlite3.connect(store.database_path, isolation_level=None) as other:
        other.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        store.database.maintain()
        assert time.monotonic() - started < 1
        other.execute("ROLLBACK")
    with store.database.write() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
