# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.cli.session import SessionStore


def test_session_store_load_normalizes_legacy_assistant_null_content(
    tmp_path: Path,
) -> None:
    store = SessionStore(directory=tmp_path)
    payload = {
        "version": 3,
        "id": "legacy-null-content",
        "conversation_entries": [
            {
                "kind": "user",
                "message": {"role": "user", "content": "hello"},
                "metadata": {},
            },
            {
                "kind": "assistant_tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "rg",
                                "arguments": '{"raw_args":"-n hello ."}',
                            },
                        }
                    ],
                },
                "metadata": {},
            },
            {
                "kind": "tool_result",
                "message": {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "content": '{"ok": true}',
                },
                "metadata": {},
            },
        ],
        "root": str(tmp_path.resolve()),
        "title": "Legacy session",
    }
    (tmp_path / "legacy-null-content.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    record = store.load("legacy-null-content")

    assert record.conversation_entries[1].message is not None
    assert record.conversation_entries[1].message.content == ""
    saved_payload = json.loads(
        (tmp_path / "legacy-null-content.json").read_text(encoding="utf-8")
    )
    assert saved_payload["conversation_entries"][1]["message"]["content"] == ""
    reloaded = store.load("legacy-null-content")
    assert reloaded.conversation_entries[1].message is not None
    assert reloaded.conversation_entries[1].message.content == ""


def test_session_store_round_trips_message_usage(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)
    message = Message.assistant("done")
    message.usage = TokenUsage(
        provider_name="test",
        model_id="gpt-test",
        input_tokens=100,
        output_tokens=20,
        reasoning_tokens=15,
        total_tokens=120,
    )

    store.save("usage-demo", [Message.user("hello"), message])

    loaded = store.load("usage-demo")

    loaded_message = loaded.conversation_entries[-1].message
    assert loaded_message is not None
    assert loaded_message.usage is not None
    assert loaded_message.usage.input_tokens == 100
    assert loaded_message.usage.reasoning_tokens == 15
    assert loaded.conversation_entries[-1].metadata["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 15,
        "total_tokens": 120,
    }
