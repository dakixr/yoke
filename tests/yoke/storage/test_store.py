"""SQLite storage core: transactions, migrations, CAS, retention and deltas."""

from __future__ import annotations

# ruff: noqa: S101,S603

import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.http.errors import ApiError
from yoke.http.models.prompt import PromptAdmissionRequest, QueuePatchRequest
from yoke.http.services.event_broker import EventService, GlobalEventBroker
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionStore, SessionTreeConflict, SessionTreeIndex
from yoke.session.admissions import AdmissionStore
from yoke.session.events import SessionEventJournal
from yoke.session.queue import prompt_queue_transaction
from yoke.session.repository.table import SessionTable
from yoke.storage import SchemaTooNewError, open_database
from yoke.storage.database import Database


def _run(code: str) -> None:
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        check=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=60,
    )


def _saved(tmp_path: Path) -> tuple[SessionStore, Path]:
    directory = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(directory)
    store.save(
        "shared", [Message.user("hello"), Message.assistant("hi")], root=workspace
    )
    return store, directory


def test_stale_daemon_queue_patch_after_cli_commit_is_a_409(tmp_path: Path) -> None:
    store, directory = _saved(tmp_path)
    service = PendingInputService(
        store,
        AdmissionStore(directory),
        EventService(SessionEventJournal(directory), GlobalEventBroker()),
        UploadService(store),
    )
    service.admit(
        "shared",
        PromptAdmissionRequest.model_validate(
            {"id": "in-1", "prompt": {"text": "first"}, "delivery": "queue"}
        ),
    )
    stale = service.queue("shared").revision
    _run(
        f"""
        from pathlib import Path
        from types import SimpleNamespace
        from yoke.cli.interactive.queue.persistence import (
            commit_prompt_queue, load_prompt_queue_state,
        )
        from yoke.session import SessionStore
        active = SimpleNamespace(store=SessionStore(Path({str(directory)!r})), id="shared")
        loaded = load_prompt_queue_state(active)
        loaded.prompts[0].paused = True
        commit_prompt_queue(active, loaded.prompts, expected_revision=loaded.revision)
        """
    )
    with pytest.raises(ApiError) as caught:
        service.patch_queue(
            "shared",
            QueuePatchRequest.model_validate(
                {
                    "expectedRevision": stale,
                    "operations": [
                        {"op": "update", "id": "in-1", "prompt": {"text": "lost"}}
                    ],
                }
            ),
        )
    assert (caught.value.status_code, caught.value.code) == (
        409,
        "queue_revision_conflict",
    )
    assert caught.value.details == {
        "expectedRevision": stale,
        "actualRevision": stale + 1,
    }
    queue = service.queue("shared")
    assert [item.prompt.text for item in queue.items] == ["first"]


def test_tree_edit_from_another_process_makes_a_stale_edit_conflict(
    tmp_path: Path,
) -> None:
    store, directory = _saved(tmp_path)
    record = store.load("shared")
    tree = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    first = record.conversation_entries[0].id
    _run(
        f"""
        from pathlib import Path
        from yoke.agent.models import ConversationEntry, Message
        from yoke.session import SessionStore
        store = SessionStore(Path({str(directory)!r}))
        summary = store.summary_record("shared")
        entry = ConversationEntry(
            kind="user", message=Message.user("other"), parent_id=summary.leaf_id
        )
        store.save_indexed_tree_navigation(
            "shared", existing_record=summary, leaf_id=entry.id,
            appended_entries=(entry,),
        )
        """
    )
    with pytest.raises(SessionTreeConflict):
        store.save_tree_delta(
            "shared", existing_record=record, tree_index=tree, leaf_id=first
        )
    entry = tree.entry_ref(first).model_copy(update={"metadata": {"label": "x"}})
    with pytest.raises(SessionTreeConflict):
        store.save_entry_metadata(
            "shared", entry, existing_record=record, tree_index=tree
        )
    # A fresh read carries the current revision and succeeds.
    fresh = store.load("shared")
    fresh_tree = SessionTreeIndex(fresh.conversation_entries, fresh.leaf_id)
    moved = store.save_tree_delta(
        "shared", existing_record=fresh, tree_index=fresh_tree, leaf_id=first
    )
    assert store.load("shared").leaf_id == first
    assert moved.tree_revision == (fresh.tree_revision or 0) + 1


def test_metadata_edit_does_not_conflict_with_tree_edits(tmp_path: Path) -> None:
    store, _ = _saved(tmp_path)
    record = store.load("shared")
    tree = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    store.set_title(
        "shared", "Concurrent title", existing_record=store.summary_record("shared")
    )
    store.save_tree_delta(
        "shared",
        existing_record=record,
        tree_index=tree,
        leaf_id=record.conversation_entries[0].id,
    )
    loaded = store.load("shared")
    assert loaded.title == "Concurrent title"
    assert loaded.leaf_id == record.conversation_entries[0].id


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "yoke.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(SchemaTooNewError, match="schema version 999"):
        Database(path)


def test_concurrent_first_open_migrates_once(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    code = f"""
        from pathlib import Path
        from yoke.storage import open_database
        open_database(Path({str(path)!r}))
        """
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    children = [
        subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)], env=env)
        for _ in range(4)
    ]
    assert [child.wait(60) for child in children] == [0, 0, 0, 0]
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    connection.close()


def test_writes_roll_back_together_and_after_commit_waits(tmp_path: Path) -> None:
    store, directory = _saved(tmp_path)
    journal = SessionEventJournal(directory)
    published: list[int] = []
    with pytest.raises(RuntimeError):
        with prompt_queue_transaction(directory, "shared") as transaction:
            transaction.snapshot.revision += 1
            transaction.commit()
            event = journal.append("shared", "test.event")
            journal.after_commit(lambda: published.append(event.seq))
            raise RuntimeError("abort")
    assert published == []
    assert journal.latest_sequence("shared") == 0

    with prompt_queue_transaction(directory, "shared") as transaction:
        event = journal.append("shared", "test.event")
        journal.after_commit(lambda: published.append(event.seq))
        assert published == []
        try:
            with store.database.write():
                store.set_title("shared", "inner")
                raise ValueError("inner failure")
        except ValueError:
            pass
        transaction.snapshot.revision += 1
        transaction.commit()
    assert published == [1]
    assert store.load("shared").title != "inner"


def test_after_commit_runs_all_callbacks_before_reporting_failure(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "callbacks.db")
    called: list[str] = []

    def fail() -> None:
        called.append("first")
        raise RuntimeError("subscriber failed")

    def succeed() -> None:
        called.append("second")

    with pytest.raises(RuntimeError, match="subscriber failed"):
        with database.write():
            database.after_commit(fail)
            database.after_commit(succeed)

    assert called == ["first", "second"]
    with database.write():
        pass


def test_retention_deletes_idle_unpinned_sessions_with_their_rows(
    tmp_path: Path,
) -> None:
    store, directory = _saved(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    for session_id, pinned in (("old", False), ("old-pinned", True)):
        store.insert_record(
            store.load("shared").model_copy(
                update={
                    "id": session_id,
                    "pinned": pinned,
                    "created_at": old,
                    "updated_at": old,
                }
            )
        )
    SessionEventJournal(directory).append("old", "test.event")
    with prompt_queue_transaction(directory, "old") as transaction:
        transaction.snapshot.revision = 3
        transaction.commit()

    store.maintain_index(force=True)

    assert {entry.id for entry in store.list_index_entries(maintain=False)} == {
        "shared",
        "old-pinned",
    }
    with store.database.read() as connection:
        for table in ("entries", "events", "session_queues"):
            assert (
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE session_id = 'old'"
                ).fetchone()[0]
                == 0
            )


def test_sql_reads_follow_appends_and_rewrites(tmp_path: Path) -> None:
    store, _ = _saved(tmp_path)
    record = store.load("shared")
    signature = store.session_file_signature("shared")
    assert signature is not None
    tree = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    entry = ConversationEntry(
        kind="user", message=Message.user("more"), parent_id=record.leaf_id
    )
    record = store.save_tree_delta(
        "shared",
        existing_record=record,
        tree_index=tree,
        leaf_id=entry.id,
        appended_entries=[entry],
    )
    from yoke.http.reads.message_index import SessionMessageIndex

    page = SessionMessageIndex(store).page(
        "shared", limit=1, order="desc", anchor_id=None
    )
    assert page is not None
    assert [item.id for item in page.entries] == [entry.id]
    assert store.load("shared").leaf_id == entry.id

    loaded = store.load("shared")
    replacement = ConversationEntry(kind="user", message=Message.user("rewritten"))
    SessionTable(store.database).write_record(
        loaded.model_copy(
            update={"conversation_entries": [replacement], "leaf_id": replacement.id}
        ),
        existing=loaded,
        appended=None,
    )
    page = SessionMessageIndex(store).page(
        "shared", limit=10, order="desc", anchor_id=None
    )
    assert page is not None
    assert [item.id for item in page.entries] == [replacement.id]
    assert not page.has_more
    rewritten = store.load("shared")
    assert [item.id for item in rewritten.conversation_entries] == [replacement.id]


def test_database_is_shared_per_file_and_reopened_after_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "yoke.db"
    first = open_database(path)
    assert open_database(path) is first
    first.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    assert open_database(path) is not first
