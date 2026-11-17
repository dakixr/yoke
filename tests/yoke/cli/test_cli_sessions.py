from __future__ import annotations

# ruff: noqa: S101,D100,D103,ANN401
import json
from pathlib import Path

import pytest

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.session_tree import InvalidCurrentError
from yoke.agent.skills.models import ActiveSkill
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.commands import handle_slash_command
from yoke.cli.render import build_console
from yoke.cli.backend.startup import run_daemon_cli
from yoke.session import SessionStore
from yoke.session.legacy.importer import import_legacy_sessions
from yoke.session.repository.models import SessionRecord

from tests.yoke.session_rows import stored_entry_rows

from .support import CaptureStream
from .terminal_daemon_support import (
    session_for,
    terminal as terminal,
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


def test_cli_auto_persists_unnamed_session_globally(
    tmp_path: Path, capsys, terminal
) -> None:
    exit_code = run_daemon_cli(
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
