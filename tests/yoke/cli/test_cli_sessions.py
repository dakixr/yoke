from __future__ import annotations

# ruff: noqa: S101,D100,D103,ANN401
import json
from pathlib import Path
from typing import Any, cast

import pytest

from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.context import CompactionPolicy, ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.session_tree import InvalidCurrentError
from yoke.agent.skills.models import ActiveSkill
from yoke.ai.providers.base import Provider
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.commands import handle_slash_command
from yoke.cli.render import build_console
from yoke.cli.backend.startup import run_daemon_cli as run_cli
from yoke.session import SessionStore
from yoke.session.legacy.importer import import_legacy_sessions
from yoke.session.repository.models import SessionRecord

from tests.yoke.session_rows import stored_entry_rows

from .support import CaptureStream
from .terminal_daemon_support import (
    session_for,
    terminal as terminal,
)


def session_payload(session_dir: Path, session_id: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        SessionStore(session_dir).load(session_id).model_dump(mode="json"),
    )


def test_importer_migrates_legacy_session_stream(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "db")
    entry = ConversationEntry(kind="user", message=Message.user("legacy hello"))
    record = SessionRecord(
        version=4,
        id="legacy-stream",
        conversation_entries=[entry],
        leaf_id=entry.id,
        root=str(tmp_path),
        title="Legacy stream",
        created_at="2026-08-01T12:00:00+00:00",
        updated_at="2026-08-01T12:01:00+00:00",
    )
    metadata = record.model_dump(mode="json", exclude={"conversation_entries"})
    path = tmp_path / "legacy" / "legacy-stream.jsonl"
    path.parent.mkdir()
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_stream", "version": 1}),
                json.dumps({"type": "session_metadata", "record": metadata}),
                json.dumps(
                    {
                        "type": "conversation_entry",
                        "entry": entry.model_dump(mode="json"),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    [result] = import_legacy_sessions(store, path.parent)
    assert result.status == "imported", result.detail
    loaded = store.load("legacy-stream")

    assert loaded.version == 5
    assert loaded.title == "Legacy stream"
    assert [message.plain_text_content for message in loaded.messages] == [
        "legacy hello"
    ]
    assert store.load("legacy-stream") == loaded


def test_index_derived_session_record_does_not_project_unloaded_history(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    saved = store.save(
        "indexed-summary",
        [Message.user("hello"), Message.assistant("world")],
        root=tmp_path,
    )

    summary = store.list()[0]

    assert summary.id == saved.id
    assert summary.leaf_id == saved.leaf_id
    assert summary.conversation_entries == []
    assert summary.messages == []
    assert [
        message.plain_text_content for message in store.load(saved.id).messages
    ] == [
        "hello",
        "world",
    ]


def test_full_session_record_still_rejects_a_leaf_without_history() -> None:
    corrupt = SessionRecord(id="corrupt", leaf_id="missing")

    with pytest.raises(InvalidCurrentError, match="empty session tree"):
        _ = corrupt.messages


def test_importer_rejects_unsupported_legacy_schema_without_rewriting(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "db")
    path = tmp_path / "legacy" / "too-old.jsonl"
    path.parent.mkdir()
    raw_text = (
        "\n".join(
            [
                json.dumps({"type": "session_stream", "version": 1}),
                json.dumps(
                    {
                        "type": "session_metadata",
                        "record": {"version": 3, "id": "too-old"},
                    }
                ),
            ]
        )
        + "\n"
    )
    path.write_text(raw_text, encoding="utf-8")

    [result] = import_legacy_sessions(store, path.parent)

    assert result.status == "failed"
    assert result.detail == "Unsupported session schema version: 3."
    assert not store.exists("too-old")
    assert path.read_text(encoding="utf-8") == raw_text


def test_cli_session_json_keeps_transcript_after_compaction(
    tmp_path: Path,
    monkeypatch,
    terminal,
) -> None:
    class CompactingProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("summary of older work")
            if "Create a concise title" in (messages[0].content or ""):
                return Message.assistant("compact title")
            return Message.assistant("done")

    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save(
        "compact-demo",
        [
            Message.user("older"),
            Message.assistant("older answer " + ("alpha " * 200)),
            Message.user("recent"),
            Message.assistant("recent answer"),
        ],
        root=tmp_path,
    )
    agent = RuntimeAgent(
        provider=CompactingProvider(),
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                max_total_tokens=300,
                keep_recent_tokens=80,
            ),
        ),
    )

    terminal.stack.callback(agent.close)
    terminal.factory = lambda _record: agent.fork(include_state=False)
    exit_code = run_cli(
        CLIArgs(
            prompt="follow-up",
            headless=True,
            session="compact-demo",
            root=str(tmp_path),
        ),
    )

    assert exit_code == 0
    record = store.load("compact-demo")
    assert any(
        "older answer alpha" in (message.content or "") for message in record.messages
    )
    assert any(entry.kind == "memory_snapshot" for entry in record.conversation_entries)
    payload = session_payload(session_dir, "compact-demo")
    assert "messages" not in payload
    assert any(
        "older answer alpha" in (entry["message"].get("content") or "")
        for entry in payload["conversation_entries"]
        if entry.get("message") is not None
    )
    assert any(
        entry["kind"] == "memory_snapshot" for entry in payload["conversation_entries"]
    )


def test_regenerate_title_uses_conversation_and_persists_title(
    tmp_path: Path,
    terminal,
) -> None:
    class TitleProvider(Provider):
        supports_image_inputs = False
        max_images_per_message = None

        def __init__(self) -> None:
            self.requests: list[list[Message]] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            assert tools == []
            self.requests.append(messages)
            return Message.assistant('"Investigate Automatic Session Titles"')

    provider = TitleProvider()
    terminal.factory = lambda record: RuntimeAgent(
        provider, [], tool_root=Path(record.root) if record.root else None
    )
    messages = [
        Message.user("The automatic title is stale."),
        Message.assistant("I will inspect it."),
    ]
    terminal.store.save("regenerate", messages, root=tmp_path, title="Old title")
    backend = terminal.backend("regenerate", root=tmp_path)
    active_session = session_for(backend)
    console_stream = CaptureStream()

    handled, returned_messages, updated_session = handle_slash_command(
        "/regenerate-title",
        agent=backend,
        active_session=active_session,
        messages=messages,
        console=build_console(console_stream),
    )

    assert handled is True
    assert returned_messages == messages
    assert updated_session.title == "Investigate Automatic Session Titles"
    assert SessionStore().load(active_session.id).title == updated_session.title
    assert [message.role for message in provider.requests[0]] == [
        "user",
        "assistant",
        "user",
    ]
    assert "no more than 6 words" in (provider.requests[0][-1].plain_text_content or "")
    assert "Updated session title" in console_stream.getvalue()


def test_cli_auto_persists_unnamed_session_globally(
    tmp_path: Path, capsys, terminal
) -> None:
    exit_code = run_cli(
        CLIArgs(prompt="persist this", headless=True, root=str(tmp_path)),
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "synthetic response"
    records = SessionStore().list(root=tmp_path)
    assert len(records) == 1
    assert records[0].root == str(tmp_path.resolve())
    assert records[0].title == "persist this"
    record = SessionStore().load(records[0].id)
    assert [message.role for message in record.messages] == [
        "user",
        "assistant",
    ]


def test_session_writer_normalizes_surrogate_pair_prompt(
    tmp_path: Path,
) -> None:
    record = SessionRecord(
        id="emoji-demo",
        conversation_entries=[
            ConversationEntry(kind="user", message=Message.user("hi \ud83d\ude0a")),
            ConversationEntry(
                kind="control",
                metadata={"raw": "broken \ud83d"},
            ),
        ],
    )
    store = SessionStore(tmp_path)
    assert store.insert_record(record)

    raw_text = "".join(
        bytes(row[4]).decode("utf-8") for row in stored_entry_rows(store, "emoji-demo")
    )
    decoded = store.load("emoji-demo")

    assert "😊" in raw_text
    assert "�" in raw_text
    assert decoded.conversation_entries[0].message is not None
    assert decoded.conversation_entries[0].message.content == "hi 😊"
    assert decoded.conversation_entries[1].metadata["raw"] == "broken �"


def test_session_save_can_clear_skills_and_skill_directories(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    store.save(
        "skills",
        [Message.user("hello")],
        active_skills=[
            ActiveSkill(
                name="demo",
                activation_id="one",
                description="demo",
                source_path="<inline>",
                content="demo",
            )
        ],
        skill_dirs=["skills"],
    )

    store.save(
        "skills",
        [Message.user("hello")],
        active_skills=[],
        skill_dirs=[],
    )

    loaded = store.load("skills")
    assert loaded.active_skills == []
    assert loaded.skill_dirs == []


def test_title_slash_command_persists_title(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    messages = [Message.user("hello")]
    store.save(
        "title-command",
        messages,
        root=tmp_path,
        title="Generated title",
    )
    backend = terminal.backend("title-command", root=tmp_path)
    active_session = session_for(backend)

    handled, _messages, _session = handle_slash_command(
        "/title Manual title",
        agent=backend,
        active_session=active_session,
        messages=messages,
        console=build_console(CaptureStream()),
    )
    record = SessionStore(session_dir).load("title-command")

    assert handled is True
    assert record.title == "Manual title"


def test_pin_slash_command_toggles_active_session(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save("pin-demo", [Message.user("hello")], root=tmp_path)
    backend = terminal.backend("pin-demo", root=tmp_path)
    active_session = session_for(backend)
    console_stream = CaptureStream()
    console = build_console(console_stream)
    writes = []
    save = SessionStore.save

    def track_save(self, *args, **kwargs):
        writes.append(args[0])
        return save(self, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "save", track_save)

    handled, messages, updated_session = handle_slash_command(
        "/pin",
        agent=backend,
        active_session=active_session,
        messages=backend.messages(),
        console=console,
    )

    assert handled is True
    assert messages == backend.messages()
    assert updated_session.record.pinned is True
    loaded = store.load("pin-demo")
    assert loaded.pinned is True
    assert loaded.messages == [Message.user("hello")]
    assert writes == []
    assert "Session pinned: pin-demo" in console_stream.getvalue()


def test_pin_slash_command_persists_new_session(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    backend = terminal.backend("new-pin", root=tmp_path)
    active_session = session_for(backend)
    writes = []
    save = SessionStore.save

    def track_save(self, *args, **kwargs):
        writes.append(args[0])
        return save(self, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "save", track_save)

    handled, _messages, updated_session = handle_slash_command(
        "/pin",
        agent=backend,
        active_session=active_session,
        messages=[],
        console=build_console(CaptureStream()),
    )

    assert handled is True
    assert updated_session.record.pinned is True
    loaded = SessionStore().load(updated_session.id)
    assert loaded.pinned is True
    assert loaded.messages == []
    assert loaded.created_at is not None
    assert writes == []


def test_session_store_fork_copies_session_without_pin(
    tmp_path: Path, monkeypatch
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save(
        "source",
        [Message.user("hello"), Message.assistant("hi")],
        root=tmp_path,
        title="Source title",
        provider_name="codex",
        model_id="gpt-5.6-sol",
    )
    store.set_pinned("source", True)
    source_record = store.summary_record("source")
    assert source_record is not None
    store.set_context_usage(
        "source",
        {
            "input_tokens": 40_000,
            "max_total_tokens": 100_000,
            "usage_percent": 40,
        },
        existing_record=source_record,
    )

    forked = store.fork("source", new_session_id_value="forked")

    assert forked.id == "forked"
    assert forked.messages == store.load("source").messages
    assert forked.title == "Source title (fork)"
    assert forked.pinned is False
    assert forked.provider_name == "codex"
    assert forked.model_id == "gpt-5.6-sol"
    assert forked.context_usage is None
    assert SessionStore(session_dir).exists("forked")


def test_fork_slash_command_switches_to_persisted_copy(
    tmp_path: Path,
    monkeypatch,
    terminal,
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    messages = [Message.user("hello"), Message.assistant("hi")]
    store.save("fork-source", messages, root=tmp_path, title="Fork title")
    backend = terminal.backend("fork-source", root=tmp_path)
    active_session = session_for(backend)
    console_stream = CaptureStream()

    handled, forked_messages, forked_session = handle_slash_command(
        "/fork",
        agent=backend,
        active_session=active_session,
        messages=messages,
        console=build_console(console_stream),
    )

    forked = SessionStore().load(forked_session.id)
    assert handled is True
    assert forked_session.id != active_session.id
    assert forked_messages == backend.messages()
    assert forked.messages == messages
    assert forked.title == "Fork title (fork)"
    assert backend.connection.session_id == forked_session.id
    assert SessionStore(session_dir).exists(forked_session.id)
    assert f"Forked session fork-source -> {forked_session.id}" in (
        console_stream.getvalue()
    )


def test_cli_fork_starts_from_source_session(
    tmp_path: Path,
    monkeypatch,
    capsys,
    terminal,
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save(
        "source-cli",
        [Message.user("hello"), Message.assistant("hi")],
        root=tmp_path,
        title="CLI title",
        provider_name="codex",
        model_id="gpt-6.1-sol",
    )

    exit_code = run_cli(
        CLIArgs(
            prompt="next",
            headless=True,
            fork_session_id="source-cli",
            root=str(tmp_path),
        ),
    )

    assert exit_code == 0
    capsys.readouterr()
    records = [
        record for record in store.list(root=tmp_path) if record.id != "source-cli"
    ]
    assert len(records) == 1
    forked = store.load(records[0].id)
    assert forked.title == "CLI title (fork)"
    assert [message.text_content() for message in forked.messages] == [
        "hello",
        "hi",
        "next",
        "synthetic response",
    ]
    assert [
        message.text_content() for message in store.load("source-cli").messages
    ] == [
        "hello",
        "hi",
    ]


def test_info_slash_command_prints_active_session_metadata(
    tmp_path: Path,
    monkeypatch,
    terminal,
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save(
        "info-demo",
        [Message.user("hello"), Message.assistant("hi")],
        root=tmp_path,
        title="Info demo",
        provider_name="codex",
        model_id="gpt-5.6-sol",
        reasoning_effort="medium",
        context_window_tokens=400_000,
    )
    backend = terminal.backend("info-demo", root=tmp_path)
    active_session = session_for(backend)
    console_stream = CaptureStream()

    handled, _messages, _session = handle_slash_command(
        "/info",
        agent=backend,
        active_session=active_session,
        messages=backend.messages(),
        console=build_console(console_stream),
    )

    output = console_stream.getvalue()
    assert handled is True
    assert "note Session info:" in output
    assert "Session id: info-demo" in output
    assert "Title: Info demo" in output
    assert "Pinned: no" in output
    assert f"Database: {session_dir / 'yoke.db'}" in output
    assert "Provider: codex" in output
    assert "Model: gpt-5.6-sol" in output
    assert "Messages: 2" in output
    assert "Conversation entries: 2" in output
    assert "Reasoning effort: medium" in output
    assert "Context window: 400000 tokens" in output
