"""The bounded handoff reader must agree with canonical JSON decoding."""

from __future__ import annotations

from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.session import SessionStore
from yoke.session.handoff import build_session_handoff
from yoke.session.handoff.reader import read_handoff_context_window


def test_handoff_resolves_equivalent_json_spellings_of_parent_id(
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
    record = store.save(
        "escaped", [], conversation_entries=[root, leaf], leaf_id=leaf.id, root=tmp_path
    )
    path = store.directory / "escaped.jsonl"
    path.write_bytes(
        path.read_bytes().replace(b'"id":"caf\xc3\xa9"', b'"id":"caf\\u00e9"')
    )
    store._update_index(record)

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
    path = store.directory / "broken-chain.jsonl"
    path.write_bytes(
        b"".join(
            line
            for line in path.read_bytes().splitlines(keepends=True)
            if b'"id":"root"' not in line
        )
    )
    store._update_index(record)

    assert read_handoff_context_window(store, record.id, recent_limit=100) is None
