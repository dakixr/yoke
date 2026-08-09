from __future__ import annotations

# ruff: noqa: D100,D103,S101

import json
from pathlib import Path

from yoke.cli.providers.state import ProviderSessionState
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime.session import save_active_session_metadata
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore


def test_provider_metadata_update_is_constant_work(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    record = SessionRecord(
        id="large-session",
        root=str(tmp_path),
        created_at="2026-08-03T00:00:00+00:00",
        updated_at="2026-08-03T00:00:00+00:00",
    )
    store._write_session_record(record)
    path = store._session_path(record.id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "ignored", "data": "x" * 16_000_000}))
        handle.write("\n")
    active = ActiveSession(record.id, tmp_path, store, record)

    save_active_session_metadata(
        active,
        ProviderSessionState(
            provider_name="demo",
            model_id="gpt-5.6-sol",
            reasoning_effort="medium",
            context_window_tokens=400_000,
        ),
    )
    last_event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert last_event == {
        "type": "metadata",
        "updated_at": active.record.updated_at,
        "provider_name": "demo",
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "context_window_tokens": 400_000,
    }
    assert active.record.model_id == "gpt-5.6-sol"
