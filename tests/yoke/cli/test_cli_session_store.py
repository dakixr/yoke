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
        (tmp_path / "legacy-null-content.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert not (tmp_path / "legacy-null-content.json").exists()
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


def test_session_store_migrates_legacy_json_file_on_startup(tmp_path: Path) -> None:
    payload = {
        "version": 4,
        "id": "startup-migrate",
        "conversation_entries": [
            {
                "kind": "user",
                "message": {"role": "user", "content": "hello"},
                "metadata": {},
            }
        ],
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "root": str(tmp_path.resolve()),
        "title": "Startup migrate",
    }
    (tmp_path / "startup-migrate.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    store = SessionStore(directory=tmp_path)

    migrated_path = tmp_path / "startup-migrate.jsonl"
    assert migrated_path.exists()
    assert not (tmp_path / "startup-migrate.json").exists()
    record = store.load("startup-migrate")
    assert record.id == "startup-migrate"
    assert record.messages[0].text_content() == "hello"
