from __future__ import annotations

# ruff: noqa: D100,D103,S101

import json
from pathlib import Path

import pytest

from yoke.cli.providers.state import ProviderSessionState
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime.session import save_active_session_metadata
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore


def test_provider_metadata_update_appends_without_reading_or_rewriting_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    record = SessionRecord(
        id="metadata-session",
        root=str(tmp_path),
        created_at="2026-08-03T00:00:00+00:00",
        updated_at="2026-08-03T00:00:00+00:00",
    )
    store._write_session_record(record)
    path = store._session_path(record.id)
    original_bytes = path.read_bytes()
    active = ActiveSession(record.id, tmp_path, store, record)
    original_open = Path.open

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Metadata updates must not load or rewrite history")

    def append_only(target: Path, mode: str = "r", *args, **kwargs):
        if target == path and mode != "a":
            raise AssertionError(f"Session history opened in {mode!r} mode")
        return original_open(target, mode, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(store, "load", fail)
        guard.setattr(store, "_write_session_record", fail)
        guard.setattr(Path, "open", append_only)
        save_active_session_metadata(
            active,
            ProviderSessionState(
                provider_name="demo",
                model_id="gpt-5.6-sol",
                reasoning_effort="medium",
                context_window_tokens=400_000,
            ),
        )

    persisted = path.read_bytes()
    assert persisted.startswith(original_bytes)
    last_event = json.loads(persisted[len(original_bytes) :])
    assert last_event == {
        "type": "metadata",
        "updated_at": active.record.updated_at,
        "provider_name": "demo",
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "context_window_tokens": 400_000,
    }
    assert active.record.model_id == "gpt-5.6-sol"
