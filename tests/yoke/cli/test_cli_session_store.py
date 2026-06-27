# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.cli.session import SessionRecord
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
    saved_lines = [
        json.loads(line)
        for line in (tmp_path / "legacy-null-content.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not (tmp_path / "legacy-null-content.json").exists()
    saved_entries = [
        line["entry"] for line in saved_lines if line["type"] == "conversation_entry"
    ]
    assert saved_entries[1]["message"]["content"] == ""
    reloaded = store.load("legacy-null-content")
    assert reloaded.conversation_entries[1].message is not None
    assert reloaded.conversation_entries[1].message.content == ""


def test_session_store_load_strips_internal_context_messages_from_tool_results(
    tmp_path: Path,
) -> None:
    store = SessionStore(directory=tmp_path)
    tool_content = json.dumps(
        {
            "ok": True,
            "path": "/tmp/screenshot.png",
            "context_messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "local_image",
                            "path": "/tmp/screenshot.png",
                            "data_url": "data:image/png;base64," + ("A" * 100_000),
                        }
                    ],
                }
            ],
        }
    )
    payload = {
        "version": 4,
        "id": "legacy-image-tool-result",
        "conversation_entries": [
            {
                "kind": "tool_result",
                "message": {
                    "role": "tool",
                    "tool_call_id": "call-image",
                    "content": tool_content,
                },
                "metadata": {},
            }
        ],
        "root": str(tmp_path.resolve()),
        "title": "Legacy image tool result",
    }
    (tmp_path / "legacy-image-tool-result.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    record = store.load("legacy-image-tool-result")

    loaded_message = record.conversation_entries[0].message
    assert loaded_message is not None
    assert loaded_message.content == '{"ok": true, "path": "/tmp/screenshot.png"}'
    saved_text = (tmp_path / "legacy-image-tool-result.jsonl").read_text(
        encoding="utf-8"
    )
    assert "context_messages" not in saved_text
    assert "data:image" not in saved_text


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


def test_session_store_migrates_legacy_json_file_on_load(tmp_path: Path) -> None:
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

    migrated_path = tmp_path / "startup-migrate.jsonl"
    assert not migrated_path.exists()

    store = SessionStore(directory=tmp_path)
    record = store.load("startup-migrate")

    assert migrated_path.exists()
    assert not (tmp_path / "startup-migrate.json").exists()
    assert record.id == "startup-migrate"
    assert record.messages[0].text_content() == "hello"


def test_session_store_migrates_snapshot_jsonl_file_on_load(tmp_path: Path) -> None:
    payload = {
        "version": 4,
        "id": "snapshot-migrate",
        "conversation_entries": [
            {
                "id": "entry-1",
                "parent_id": None,
                "kind": "user",
                "message": {"role": "user", "content": "hello"},
                "metadata": {},
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        "leaf_id": "entry-1",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "root": str(tmp_path.resolve()),
        "title": "Snapshot migrate",
    }
    (tmp_path / "snapshot-migrate.jsonl").write_text(
        json.dumps({"type": "session_record", "version": 1}, separators=(",", ":"))
        + "\n"
        + json.dumps(payload, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    store = SessionStore(directory=tmp_path)
    record = store.load("snapshot-migrate")

    migrated_lines = (
        (tmp_path / "snapshot-migrate.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert json.loads(migrated_lines[0]) == {"type": "session_stream", "version": 1}
    assert [json.loads(line)["type"] for line in migrated_lines[1:]] == [
        "session_metadata",
        "conversation_entry",
    ]
    assert record.id == "snapshot-migrate"
    assert record.messages[0].text_content() == "hello"


def test_session_store_appends_new_entries_to_jsonl_stream(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)
    store.save("append-demo", [Message.user("hello")], title="Append demo")
    path = tmp_path / "append-demo.jsonl"
    initial_lines = path.read_text(encoding="utf-8").splitlines()

    store.save(
        "append-demo",
        [Message.user("hello"), Message.assistant("done")],
        title="Append demo updated",
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[: len(initial_lines)] == initial_lines
    assert len(lines) == len(initial_lines) + 2
    loaded = store.load("append-demo")
    assert [message.text_content() for message in loaded.messages] == ["hello", "done"]
    assert loaded.title == "Append demo updated"


def test_session_store_ignores_truncated_final_jsonl_event(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)
    store.save("truncated-demo", [Message.user("hello")], title="Truncated demo")
    path = tmp_path / "truncated-demo.jsonl"
    with path.open("a", encoding="utf-8") as file:
        file.write('{"type":"conversation_entry","entry":{"message":"unterminated')

    loaded = store.load("truncated-demo")

    assert loaded.id == "truncated-demo"
    assert loaded.title == "Truncated demo"
    assert [message.text_content() for message in loaded.messages] == ["hello"]


def test_session_store_loads_legacy_append_only_entry_stream(tmp_path: Path) -> None:
    entry = SessionRecord(
        id="legacy-stream",
        conversation_entries=[],
    )
    store = SessionStore(directory=tmp_path)
    template_path = store.save("template", [Message.user("hello")])
    template_record = store.load("template")
    payload = template_record.conversation_entries[0].model_dump_json()
    (tmp_path / "legacy-stream.jsonl").write_text(
        json.dumps({"id": entry.id, "title": "Legacy stream"}) + "\n" + payload + "\n",
        encoding="utf-8",
    )
    template_path.unlink()
    (tmp_path / "index.json").unlink()

    loaded = store.load("legacy-stream")

    assert loaded.id == "legacy-stream"
    assert loaded.title == "Legacy stream"
    assert loaded.messages[0].text_content() == "hello"
