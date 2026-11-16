from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.session.repository.metadata import update_loaded_provider_state
from yoke.session.repository import SessionRecord
from yoke.session import SessionStore

from tests.yoke.session_rows import stored_entry_bytes


def test_provider_metadata_update_writes_only_metadata_without_reading_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    record = SessionRecord(
        id="metadata-session",
        root=str(tmp_path),
        created_at="2026-08-03T00:00:00+00:00",
        updated_at="2026-08-03T00:00:00+00:00",
        conversation_entries=[
            ConversationEntry(kind="user", message=Message.user("history"))
        ],
    )
    assert store.insert_record(record)
    original_entries = stored_entry_bytes(store, record.id)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Metadata updates must not load or rewrite history")

    with monkeypatch.context() as guard:
        guard.setattr(store, "load", fail)
        guard.setattr(store, "save", fail)
        guard.setattr(store, "read_entries", fail)
        updated = update_loaded_provider_state(
            store,
            record,
            root=tmp_path,
            title=record.title,
            provider_name="demo",
            model_id="gpt-5.6-sol",
            reasoning_effort="medium",
            context_window_tokens=400_000,
        )

    assert stored_entry_bytes(store, record.id) == original_entries
    summary = store.summary_record(record.id)
    assert summary is not None
    assert summary.updated_at == updated.updated_at
    assert summary.provider_name == "demo"
    assert summary.model_id == "gpt-5.6-sol"
    assert summary.reasoning_effort == "medium"
    assert summary.context_window_tokens == 400_000
    assert updated.model_id == "gpt-5.6-sol"
