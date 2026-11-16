"""Provider wire serializers remain exact across storage, including raw usage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.ai.providers.codex.subscription.messages import convert_messages
from yoke.ai.providers.openai_compat.content import serialize_message_for_openai
from yoke.session import SessionRecord, SessionStore
from yoke.session.repository.table import SessionTable

from .test_oracle import GOLDEN


@pytest.mark.parametrize("fixture", sorted(GOLDEN.glob("*.json")), ids=lambda p: p.name)
def test_stored_fixture_messages_have_identical_provider_wire_requests(
    fixture: Path, tmp_path: Path
) -> None:
    """All frozen scenarios preserve both Codex and OpenAI serializer bytes.

    Historical usage.raw deliberately disappears from the Message model but
    never reaches either provider's wire request. Images remain fixed tiny PNGs.
    """
    case = json.loads(fixture.read_bytes())
    store = SessionStore(tmp_path / "store")
    table = SessionTable(store.database)
    calls = case["calls"] + case["live_continuation"] + case["resumed_continuation"]
    for index, call in enumerate(calls):
        original = [Message.model_validate_json(raw) for raw in call["messages_json"]]
        entries = [
            ConversationEntry(id=str(i), kind="instruction", message=message)
            for i, message in enumerate(original)
        ]
        owner = f"call-{index}"
        assert table.insert_record(
            SessionRecord(id=owner, conversation_entries=entries)
        )
        loaded = [entry.message for entry in store.load(owner).conversation_entries]
        assert all(message is not None for message in loaded)
        for message in loaded:
            assert message is not None
        restored = [message for message in loaded if message is not None]
        assert json.dumps(convert_messages(restored), ensure_ascii=False) == json.dumps(
            convert_messages(original), ensure_ascii=False
        )
        assert json.dumps(
            [serialize_message_for_openai(message) for message in restored],
            ensure_ascii=False,
        ) == json.dumps(
            [serialize_message_for_openai(message) for message in original],
            ensure_ascii=False,
        )
