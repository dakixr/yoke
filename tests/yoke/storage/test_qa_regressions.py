"""Adversarial regressions for SQLite storage lifetime and retention."""

from __future__ import annotations

# ruff: noqa: S101

import asyncio
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import UploadFile

from yoke.agent.models import ConversationEntry, Message
from yoke.http.errors import ApiError
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.reads.cache import SessionReadCache
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.reads.model import SessionReadModel
from yoke.http.services.event_broker import EventService, GlobalEventBroker
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.session_catalog import SessionCatalog
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionStore
from yoke.session.events import SessionEventJournal
from yoke.session.legacy.importer import import_legacy_sessions
from yoke.session.queue import load_prompt_queue_snapshot, prompt_queue_transaction
from yoke.session.workspace import relocate_session_workspace


def saved(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "sessions", migrate=True)
    store.save("s", [Message.user("a"), Message.assistant("b")], root=tmp_path)
    return store


def pending_service(store: SessionStore) -> PendingInputService:
    return PendingInputService(
        store,
        AdmissionStore(store.directory),
        EventService(SessionEventJournal(store.directory), GlobalEventBroker()),
        UploadService(store),
    )


def test_abandoned_iterator_does_not_stale_a_pooled_reader(tmp_path: Path) -> None:
    store = saved(tmp_path)
    iterator = store.iter_entries("s")
    with ThreadPoolExecutor(1) as executor:
        executor.submit(next, iterator).result()
        try:
            store.save(
                "s",
                [Message.user("a"), Message.assistant("b"), Message.user("new")],
            )
            assert (
                len(executor.submit(store.load, "s").result().conversation_entries) == 3
            )
            # The iterator still owns its original, consistent snapshot.
            assert len(executor.submit(list, iterator).result()) == 1
        finally:
            executor.submit(iterator.close).result()
        with sqlite3.connect(store.database_path, isolation_level=None) as connection:
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )


@pytest.mark.parametrize("field", ["root", "title", "provider_name", "model_id"])
def test_surrogate_text_columns_save_and_update(tmp_path: Path, field: str) -> None:
    store = SessionStore(tmp_path / "sessions", migrate=True)
    bad = "bad-\udcff-\ud83d\ude00"
    value = os.fsdecode(bytes(tmp_path) + b"/bad-\xff") if field == "root" else bad
    if field == "root":
        try:
            os.mkdir(value)
        except OSError as error:
            pytest.skip(
                "This filesystem cannot name a directory with non-UTF-8 bytes, "
                f"as on macOS/APFS and Windows: {error}"
            )
    record = cast(Any, store).save("s", [Message.user("hi")], **{field: value})
    expected = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    assert getattr(store.load("s"), field) == expected
    updated = record.model_copy(update={field: value + "-updated"})
    store.update_metadata(updated, [field])
    assert getattr(store.load("s"), field) == expected + "-updated"


@pytest.mark.parametrize("mutation", ["title", "pin", "archive", "context", "direct"])
def test_metadata_edit_never_resurrects_deleted_session(
    tmp_path: Path, mutation: str
) -> None:
    store = saved(tmp_path)
    stale = store.summary_record("s")
    assert stale is not None and stale.leaf_id is not None
    assert SessionStore(store.directory, migrate=True).delete("s")
    with pytest.raises(ValueError, match="Session not found"):
        if mutation == "title":
            store.set_title("s", "new", existing_record=stale)
        elif mutation == "pin":
            store.set_pinned("s", True, existing_record=stale)
        elif mutation == "archive":
            store.set_archived("s", True, existing_record=stale)
        elif mutation == "context":
            store.set_context_usage("s", {"input": 3}, existing_record=stale)
        else:
            store.update_metadata(stale, ["title"])
    assert not store.exists("s")


def test_surrogate_entry_columns_match_sanitized_blob(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", migrate=True)
    entry = ConversationEntry(
        id="entry-\ud800",
        kind="tool_result",
        message=Message.tool("call-\ud800", "ok"),
    )
    store.save("s", [], conversation_entries=[entry], leaf_id=entry.id)
    loaded = store.load("s")
    assert loaded.leaf_id == "entry-�"
    assert loaded.conversation_entries[0].id == "entry-�"
    with store.database.read() as connection:
        assert connection.execute("SELECT id, call_id FROM entries").fetchone() == (
            "entry-�",
            "call-�",
        )


def test_archive_rechecks_queue_after_concurrent_admission(tmp_path: Path) -> None:
    store = saved(tmp_path)
    service = pending_service(store)
    reads = SessionReadModel(store, SessionMessageIndex(store), SessionReadCache(store))
    catalog = SessionCatalog(store, None, reads)

    def admit() -> None:
        service.admit(
            "s",
            PromptAdmissionRequest.model_validate(
                {"id": "pending", "prompt": {"text": "hello"}, "delivery": "queue"}
            ),
        )

    with pytest.raises(ApiError) as caught:
        catalog.patch_session(
            "s",
            title_set=False,
            title=None,
            pinned_set=False,
            pinned=None,
            archived_set=True,
            archived=True,
            before_mutation=admit,
        )
    assert caught.value.code == "session_has_pending_work"
    assert store.load("s").archived_at is None
    assert len(load_prompt_queue_snapshot(store.directory, "s").prompts) == 1


def test_relocation_decodes_history_after_releasing_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = saved(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    original = store.load
    history = original("s").conversation_entries

    def load(session_id: str):
        assert not store.database.in_write(), "history decoded under writer lock"
        return original(session_id)

    monkeypatch.setattr(store, "load", load)
    moved = relocate_session_workspace(store, "s", target)
    assert moved.root == str(target)
    assert store.load("s").conversation_entries == history


def test_upload_pin_and_delete_rollback_preserve_native_file(tmp_path: Path) -> None:
    store = saved(tmp_path)
    uploads = UploadService(store)
    info = asyncio.run(
        uploads.create(
            UploadFile(BytesIO(b"native attachment"), filename="note.txt"),
            session_id="s",
        )
    )
    reference = dict(session_id="s", name=info.name, mime=info.mime)
    path = uploads.resolve(info.uri, **reference)
    with pytest.raises(RuntimeError, match="abort"):
        with prompt_queue_transaction(store.directory, "s"):
            uploads.pin(info.uri, **reference)
            raise RuntimeError("abort")
    with store.database.read() as connection:
        assert connection.execute("SELECT durable FROM uploads").fetchone() == (0,)
    with pytest.raises(RuntimeError, match="abort"):
        with prompt_queue_transaction(store.directory, "s"):
            assert uploads.delete_bound_upload(info.uri, session_id="s")
            assert path.exists()
            raise RuntimeError("abort")
    assert path.read_bytes() == b"native attachment"
    assert uploads.resolve(info.uri, **reference) == path
    with prompt_queue_transaction(store.directory, "s"):
        assert uploads.delete_bound_upload(info.uri, session_id="s")
        assert path.exists()
    assert not path.exists()


def test_imported_old_session_retained_until_used_then_normal_retention(
    tmp_path: Path,
) -> None:
    store = saved(tmp_path)
    source = tmp_path / "legacy"
    source.mkdir()
    old = "2000-01-01T00:00:00+00:00"
    record = store.load("s").model_copy(
        update={"id": "imported", "created_at": old, "updated_at": old}
    )
    (source / "imported.jsonl").write_text(record.model_dump_json())
    [result] = import_legacy_sessions(store, source)
    assert result.status == "imported", result
    for _ in range(2):
        store.maintain_index(force=True)
        assert store.exists("imported"), "fresh import was immediately pruned"
    imported = store.load("imported")
    assert imported.created_at == imported.updated_at == old
    store.save(
        "imported",
        [*imported.messages, Message.user("resumed")],
        existing_record=imported,
    )
    store.maintain_index(force=True)
    assert store.exists("imported")
    with store.database.write() as connection:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = 'imported'", (old,)
        )
    store.maintain_index(force=True)
    assert not store.exists("imported")


def test_idle_maintenance_reclaims_orphan_pages(tmp_path: Path) -> None:
    store = saved(tmp_path)
    store.save("large", [Message.user("x" * 2_000_000)])
    with store.database.read() as connection:
        before = connection.execute("PRAGMA page_count").fetchone()[0]
    store.delete("large")
    for _ in range(4):
        store.database.maintain()
    with store.database.read() as connection:
        after = connection.execute("PRAGMA page_count").fetchone()[0]
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert after < before - 400
