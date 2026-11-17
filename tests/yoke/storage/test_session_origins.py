"""Legacy provenance uses explicit creating-client evidence, never titles."""

import json
from pathlib import Path

import pytest

from yoke.session import SessionStore
from yoke.session.legacy.importer import import_legacy_sessions
from yoke.session.origins import infer_origin, repair_origins


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"origin": "web"}, "web"),
        ({"client": {"kind": "acp"}}, "acp"),
        ({"client": {"kind": "cli"}}, "cli"),
        ({"client": {"kind": "sdk"}}, "sdk"),
        ({"client_session": {"kind": "web"}}, "web"),
        ({"title": "ACP session"}, "unknown"),
        ({"origin": "anything"}, "unknown"),
        ({}, "unknown"),
        ({"origin": "web", "client": {"kind": "acp"}}, "unknown"),
    ],
)
def test_inference(metadata, expected):
    assert infer_origin(metadata) == expected


def test_import_reads_record_and_index_client_metadata(tmp_path: Path):
    source = tmp_path / "legacy"
    source.mkdir()
    store = SessionStore(tmp_path / "db", migrate=True)
    for sid, extra in [
        ("record", {"client": {"kind": "acp"}}),
        ("index", {}),
        ("unknown", {"title": "ACP: don't guess"}),
    ]:
        (source / f"{sid}.jsonl").write_text(
            json.dumps(
                {
                    "id": sid,
                    "version": 5,
                    "created_at": "2000-01-01",
                    **extra,
                }
            )
        )
    (source / "index.json").write_text(
        json.dumps({"sessions": {"index": {"id": "index", "client": {"kind": "web"}}}})
    )
    assert {x.status for x in import_legacy_sessions(store, source)} == {"imported"}
    assert {sid: store.load(sid).origin for sid in ("record", "index", "unknown")} == {
        "record": "acp",
        "index": "web",
        "unknown": "unknown",
    }


def test_migration_preserves_native_origins_and_marks_ambiguous_imports(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    for sid, origin in [
        ("native", "cli"),
        ("web", "web"),
        ("acp", "acp"),
        ("sdk", "sdk"),
        ("imported", "cli"),
        ("resumed-import", "cli"),
    ]:
        store.save(sid, [], origin=origin)
    from yoke.session.events import SessionEventJournal

    SessionEventJournal(store.directory).append("native", "session.created")
    with store.database.write() as db:
        db.execute("UPDATE sessions SET import_hold=1 WHERE id='imported'")
        repair_origins(db)
    assert store.load("native").origin == "cli"
    for origin in ("web", "acp", "sdk"):
        assert store.load(origin).origin == origin
    for sid in ("imported", "resumed-import"):
        assert store.load(sid).origin == "unknown"


def test_migration_uses_explicit_creation_metadata_when_retained(tmp_path: Path):
    store = SessionStore(tmp_path, migrate=True)
    for sid in ("known", "conflict"):
        store.save(sid, [])
    from yoke.session.events import SessionEventJournal

    events = SessionEventJournal(store.directory)
    events.append("known", "session.created", data={"client": {"kind": "acp"}})
    events.append(
        "conflict", "session.created", data={"origin": "web", "client": {"kind": "acp"}}
    )
    with store.database.write() as db:
        db.execute("UPDATE sessions SET import_hold=1")
        repair_origins(db)
    assert store.load("known").origin == "acp"
    assert store.load("conflict").origin == "unknown"
