"""Keep known sessions visible through transient index refresh errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from yoke.agent.models import Message
from yoke.cli.session import SessionStore
from yoke.cli.session.writer import append_session_metadata


def test_transient_summary_failure_retains_prior_index_until_next_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("known", [Message.user("hello")], root=tmp_path, title="old title")
    previous = store.index_entry("known")
    path = store.directory / "known.jsonl"
    append_session_metadata(path, {"title": "new title"})
    original = path.read_bytes()

    def denied(_session_id: str):
        raise PermissionError("temporarily unreadable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_load_summary_for_index", denied)
        store.maintain_index(force=True)
        assert store.index_entry("known") == previous
        assert [record.title for record in store.list()] == ["old title"]

    store.maintain_index(force=True)
    refreshed = store.index_entry("known")
    assert refreshed is not None
    assert refreshed.title == "new title"
    assert path.read_bytes() == original

    path.unlink()
    store.maintain_index(force=True)
    assert store.index_entry("known") is None


def test_unreadable_new_session_does_not_fabricate_an_index_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.directory.mkdir()
    (store.directory / "unknown.jsonl").write_text("not yet readable", encoding="utf-8")

    def denied(_session_id: str):
        raise PermissionError("temporarily unreadable")

    monkeypatch.setattr(store, "_load_summary_for_index", denied)
    store.maintain_index(force=True)

    assert store.list() == []


def test_invalid_session_is_still_omitted_from_the_index(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save("broken", [Message.user("hello")], root=tmp_path)
    path = store.directory / "broken.jsonl"
    path.write_text("invalid session", encoding="utf-8")

    store.maintain_index(force=True)

    assert store.index_entry("broken") is None
    assert path.read_text() == "invalid session"
