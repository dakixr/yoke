from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path

from yoke.agent.models import Message
from yoke.session import SessionStore


def test_canonical_session_noop_round_trip_preserves_bytes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("fixture", [Message.user("hello"), Message.assistant("world")])
    path = store.session_path("fixture")
    original = path.read_bytes()

    record = store.load("fixture")
    store.write_session_record(record, existing_record=record)

    assert path.read_bytes() == original


def test_metadata_patch_keeps_canonical_layout_and_reloads(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("fixture", [Message.user("hello")])
    path = store.session_path("fixture")
    before = path.read_bytes()

    store.set_title("fixture", "A title")

    after = path.read_bytes()
    assert after.startswith(before)
    assert store.load("fixture").title == "A title"
