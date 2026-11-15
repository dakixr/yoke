"""The bounded handoff reader must agree with canonical JSON decoding."""

from __future__ import annotations

from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.session import SessionStore
from yoke.session.handoff import build_session_handoff
from yoke.session.handoff.reader import read_handoff_context_window


def test_handoff_fast_path_matches_canonical_for_non_ascii_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path / "sessions")
    root = ConversationEntry(
        id="café", kind="user", message=Message.user("Original request")
    )
    leaf = ConversationEntry(
        id="leaf",
        parent_id=root.id,
        kind="assistant",
        message=Message.assistant("Latest answer"),
    )
    store.save(
        "escaped", [], conversation_entries=[root, leaf], leaf_id=leaf.id, root=tmp_path
    )

    fast = build_session_handoff("escaped", store=store)
    with monkeypatch.context() as patch:
        patch.setattr(
            "yoke.session.handoff.builder.read_handoff_context_window",
            lambda *_args, **_kwargs: None,
        )
        canonical = build_session_handoff("escaped", store=store)

    assert [message.content for message in fast.messages] == [
        "Original request",
        "Latest answer",
    ]
    assert fast.model_dump() == canonical.model_dump()


def test_fast_handoff_rejects_an_unproven_parent_chain(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    root = ConversationEntry(
        id="root", kind="user", message=Message.user("Original request")
    )
    leaf = ConversationEntry(
        id="leaf",
        parent_id=root.id,
        kind="assistant",
        message=Message.assistant("Latest answer"),
    )
    record = store.save(
        "broken-chain",
        [],
        conversation_entries=[root, leaf],
        leaf_id=leaf.id,
        root=tmp_path,
    )
    with store.database.write() as connection:
        connection.execute(
            "DELETE FROM entries WHERE session_id = ? AND id = 'root'", (record.id,)
        )

    assert read_handoff_context_window(store, record.id, recent_limit=100) is None
