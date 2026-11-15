from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path

from yoke.agent.models import Message
from yoke.session import SessionStore

from tests.yoke.session_rows import stored_entry_bytes, stored_session_bytes


def test_noop_save_round_trip_leaves_stored_entries_untouched(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("fixture", [Message.user("hello"), Message.assistant("world")])
    original = stored_entry_bytes(store, "fixture")

    record = store.load("fixture")
    store.save(
        "fixture",
        [],
        conversation_entries=list(record.conversation_entries),
        leaf_id=record.leaf_id,
        existing_record=record,
    )

    assert stored_entry_bytes(store, "fixture") == original
    assert store.load("fixture").conversation_entries == record.conversation_entries


def test_metadata_patch_only_touches_the_session_row(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("fixture", [Message.user("hello")])
    entries = stored_entry_bytes(store, "fixture")
    row = stored_session_bytes(store, "fixture")

    store.set_title("fixture", "A title")

    assert stored_entry_bytes(store, "fixture") == entries
    assert stored_session_bytes(store, "fixture") != row
    assert store.load("fixture").title == "A title"
