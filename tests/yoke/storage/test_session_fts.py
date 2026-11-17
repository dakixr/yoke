"""Session search follows committed storage mutations without indexing carriers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from functools import partial
from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.http.reads.queries import SessionQueries
from yoke.session import SessionStore
from yoke.storage.database import Database, migrations


def search(store: SessionStore, text: str, **filters):
    return SessionQueries(store).sessions(
        directory=None,
        search=text,
        pinned=None,
        archived=None,
        order="updatedDesc",
        limit=50,
        anchor_id=None,
        **filters,
    )


def test_search_tracks_insert_rewrite_fork_title_delete_and_rollback(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    store.save("s", [Message.user("firstneedle"), Message.assistant("answerneedle")])
    assert [x.id for x in search(store, "firstneedle")[0]] == ["s"]
    assert [x.id for x in search(store, "answerneedle")[0]] == ["s"]
    from yoke.session.repository.table import SessionTable

    replacement = ConversationEntry(
        id="replacement", kind="user", message=Message.user("replacementneedle")
    )
    existing = store.load("s")
    SessionTable(store.database).write_record(
        existing.model_copy(
            update={"conversation_entries": [replacement], "leaf_id": replacement.id}
        ),
        existing=existing,
        appended=None,
    )
    assert search(store, "firstneedle")[1] == 0
    store.fork("s", new_session_id_value="f", materialize_result=False)
    assert {x.id for x in search(store, "replacementneedle")[0]} == {"s", "f"}
    store.set_title("f", "titleneedle")
    assert search(store, "titleneedle")[0][0].id == "f"
    store.set_title("f", "other")
    assert search(store, "titleneedle")[1] == 0
    with pytest.raises(RuntimeError), store.database.write():
        store.save("rollback", [Message.user("rollbackneedle")])
        raise RuntimeError("abort")
    assert search(store, "rollbackneedle")[1] == 0
    store.delete("s")
    assert [x.id for x in search(store, "replacementneedle")[0]] == ["f"]
    store.delete("f")
    assert search(store, "replacementneedle")[1] == 0


def test_only_message_text_is_indexed_and_snippet_is_highlighted(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    store.save(
        "s",
        [
            Message.user(
                [
                    {"type": "text", "text": "before uniqueneedle after <script>"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2VuZWVkbGU="},
                    },
                ]
            ),
            Message.tool("call", "toolneedle"),
        ],
    )
    page, _, _ = search(store, "uniqueneedle")
    assert page[0].search_snippet is not None
    assert "<mark>uniqueneedle</mark>" in page[0].search_snippet
    assert len(page[0].search_snippet) < 300
    for excluded in ("toolneedle", "imagen eedle", "aW1hZ2VuZWVkbGU", "image_url"):
        assert search(store, excluded)[1] == 0


def test_relevance_safe_input_paging_and_filters(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    for sid, title, text, origin, root in [
        ("best", "otter", "short", "web", tmp_path),
        ("body", "other", "otter " + "long " * 100, "cli", tmp_path),
        ("archived", "otter", "short", "acp", tmp_path),
        ("elsewhere", "otter", "short", "unknown", tmp_path / "other"),
    ]:
        store.save(sid, [Message.user(text)], title=title, origin=origin, root=root)
    store.set_archived("archived", True)
    queries = SessionQueries(store)
    page = partial(
        queries.sessions,
        directory=str(tmp_path),
        search="otter",
        pinned=None,
        archived=False,
        order="updatedDesc",
        limit=1,
    )
    first, total, more = page(anchor_id=None)
    assert [x.id for x in first] == ["best"]
    assert total == 2 and more
    second, total, more = page(anchor_id="best")
    assert [x.id for x in second] == ["body"]
    assert total == 2 and not more
    assert page(origin="web", anchor_id=None)[1] == 1
    assert search(store, "otter", origin="unknown")[0][0].id == "elsewhere"
    for text in ['"', "*", 'otter" * OR NEAR(', "otter*", '"otter"', "---", ""]:
        search(store, text)
    assert search(store, '"otter"*')[1] == 4
    assert search(store, "best")[0][0].id == "best"  # ID lookup survives.
    assert search(store, str(tmp_path))[1] == 4  # Workspace lookup survives.


def test_retention_and_compaction_rewrites_keep_search_current(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    record = store.save("old", [Message.user("retentionneedle")])
    record.created_at = record.updated_at = "2000-01-01T00:00:00+00:00"
    # Importing an old record is enough to exercise retention's storage path.
    store.delete("old")
    store.insert_record(record)
    from yoke.session.repository.table import SessionTable

    assert SessionTable(store.database).prune(1) == ["old"]
    assert search(store, "retentionneedle")[1] == 0
    store.save(
        "compact", [Message.user("discardneedle"), Message.assistant("keepneedle")]
    )
    summary = ConversationEntry(
        id="summary",
        kind="compaction_summary",
        metadata={"summary_text": "summaryneedle"},
    )
    # Runtime compaction persists its rewritten tree through this same save seam.
    from yoke.session.repository.table import SessionTable

    existing = store.load("compact")
    SessionTable(store.database).write_record(
        existing.model_copy(
            update={"conversation_entries": [summary], "leaf_id": summary.id}
        ),
        existing=existing,
        appended=None,
    )
    assert search(store, "discardneedle")[1] == 0
    assert search(store, "keepneedle")[1] == 0


def v6_fixture(path: Path):
    with sqlite3.connect(path) as db:
        db.create_function(
            "yoke_sha256", 1, lambda raw: hashlib.sha256(raw).hexdigest()
        )
        db.executescript("BEGIN;\n" + "\n".join(sql for _, sql in migrations()[:6]))
        db.execute("PRAGMA user_version=6")
        db.execute(
            "INSERT INTO sessions(id,title,import_hold) VALUES ('legacy','otter title',1)"
        )
        raw = json.dumps(
            {
                "id": "u",
                "kind": "user",
                "message": {"role": "user", "content": "backfillneedle"},
            }
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        db.execute("INSERT INTO blobs VALUES (?,?)", (digest, raw))
        db.execute(
            "INSERT INTO entries(session_id,seq,id,kind,role,message_sha256,rev) VALUES ('legacy',1,'u','user','user',?,1)",
            (digest,),
        )


def test_v7_backfill_is_atomic_backed_up_and_idempotent(tmp_path: Path, monkeypatch):
    path = tmp_path / "v6.db"
    v6_fixture(path)
    import yoke.storage.search as fts

    original = fts.backfill

    def interrupted(db):
        original(db)
        raise KeyboardInterrupt

    monkeypatch.setattr(fts, "backfill", interrupted)
    with pytest.raises(KeyboardInterrupt):
        Database(path, migrate=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (6,)
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_search'"
        ).fetchall()
    monkeypatch.setattr(fts, "backfill", original)
    db = Database(path, migrate=True)
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM session_search").fetchall()
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute("SELECT origin FROM sessions").fetchone() == (
            "unknown",
        )
        assert connection.execute(
            "SELECT session_id FROM session_search WHERE session_search MATCH 'backfillneedle'"
        ).fetchall() == [("legacy",)]
    db.close()
    with sqlite3.connect(path.with_name("v6.db.pre-v7")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone() == (6,)
    db = Database(path, migrate=True)
    with db.read() as connection:
        assert connection.execute("SELECT * FROM session_search").fetchall() == rows
    with db.write() as connection:
        fts.backfill(connection)
        assert connection.execute("SELECT * FROM session_search").fetchall() == rows
    db.close()


def test_image_storage_compaction_preserves_search_and_entry_text(
    tmp_path: Path, monkeypatch
):
    from yoke.storage.compactor import compact_batch
    from yoke.storage.blob_codec import decode_blob

    store = SessionStore(tmp_path, migrate=True)
    record = store.save(
        "s",
        [
            Message.user(
                [
                    {"type": "text", "text": "codectextneedle"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                    },
                ]
            )
        ],
    )
    with store.database.write() as db:
        digest, data = db.execute(
            "SELECT b.sha256,b.data FROM blobs b JOIN entries e ON e.message_sha256=b.sha256"
        ).fetchone()
        raw = decode_blob(db, digest, data)
        assert raw is not None
        before = json.loads(raw)["message"]["content"][0]
        db.execute(
            "UPDATE sessions SET created_at='2000-01-01', updated_at='2000-01-01', last_user_at=NULL"
        )
        db.execute("DELETE FROM blob_compaction")
    monkeypatch.setattr(
        "yoke.storage.entry_transform.reencode_image",
        lambda raw: "data:image/png;base64,b3RoZXI=",
    )
    assert compact_batch(store.database, limit=2).entries_rewritten == 1
    assert search(store, "codectextneedle")[0][0].id == record.id
    message = store.load("s").conversation_entries[0].message
    assert message is not None and isinstance(message.content, list)
    from yoke.agent.models import MessageTextContentPart

    assert isinstance(message.content[0], MessageTextContentPart)
    assert message.content[0].text == before["text"]


def test_legacy_tool_injections_are_excluded_after_classification(tmp_path: Path):
    from tests.yoke.http.test_session_view_regressions import _legacy_attach_image_tree

    store = SessionStore(tmp_path, migrate=True)
    exported = _legacy_attach_image_tree().export_for_persistence()
    store.save(
        "s", [], conversation_entries=list(exported.entries), leaf_id=exported.leaf_id
    )
    assert search(store, "private legacy verification")[1] == 0
    assert search(store, "verified")[1] == 1


def test_proven_tree_append_and_branch_compaction_remain_searchable(tmp_path: Path):
    from yoke.session import SessionTreeIndex

    store = SessionStore(tmp_path, migrate=True)
    record = store.save("s", [Message.user("firstbranchneedle")])
    index = SessionTreeIndex(record.conversation_entries, record.leaf_id)
    assistant = ConversationEntry(
        id="answer",
        kind="assistant",
        parent_id=record.leaf_id,
        message=Message.assistant("appendneedle"),
    )
    record = store.save_tree_delta(
        "s",
        existing_record=record,
        tree_index=index,
        leaf_id=assistant.id,
        appended_entries=[assistant],
    )
    assert search(store, "appendneedle")[1] == 1
    summary = ConversationEntry(
        id="checkpoint",
        kind="memory_snapshot",
        parent_id=assistant.id,
        metadata={"summary_text": "metadataneedle"},
    )
    store.save(
        "s",
        [],
        conversation_entries=[*record.conversation_entries, summary],
        leaf_id=summary.id,
    )
    assert search(store, "appendneedle")[1] == 1
    assert search(store, "firstbranchneedle")[1] == 1
    assert search(store, "metadataneedle")[1] == 0


def test_snippet_stays_short_for_unbroken_text(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    store.save(
        "s", [Message.user("prefix " + "x" * 2000 + " tailneedle " + "y" * 2000)]
    )
    snippet = search(store, "tailneedle")[0][0].search_snippet
    assert snippet is not None and len(snippet) <= 280
    assert "<mark>tailneedle</mark>" in snippet


def test_v6_encoded_image_backfill_reads_text_without_reconstructing_images(
    tmp_path: Path, monkeypatch
):
    from yoke.storage.blob_codec import prepare_blob

    path = tmp_path / "v6.db"
    v6_fixture(path)
    raw = json.dumps(
        {
            "id": "image",
            "kind": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "encodedneedle"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                    },
                ],
            },
        }
    ).encode()
    with sqlite3.connect(path) as connection:
        digest = prepare_blob(raw).insert(connection)
        connection.execute(
            "INSERT INTO entries(session_id,seq,id,kind,role,message_sha256,rev) VALUES ('legacy',2,'image','user','user',?,1)",
            (digest,),
        )

    def forbid(*args):
        raise AssertionError("Backfill must not reconstruct image carriers")

    monkeypatch.setattr("yoke.storage.blob_codec.decode_blob", forbid)
    db = Database(path, migrate=True)
    with db.read() as connection:
        assert connection.execute(
            "SELECT text FROM session_search WHERE session_search MATCH 'encodedneedle'"
        ).fetchall() == [("encodedneedle",)]
        assert connection.execute(
            "SELECT count(*) FROM session_search WHERE session_search MATCH 'aW1hZ2U'"
        ).fetchone() == (0,)
    db.close()
