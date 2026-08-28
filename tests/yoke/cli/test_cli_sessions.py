from __future__ import annotations

# ruff: noqa: F403,F405,S101,D100,D103,ANN401

import json
from typing import Any
from typing import cast

from yoke.agent.models import ConversationEntry
from yoke.cli.session.io import decode_session_record
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.writer import write_session_record
from yoke.cli.interactive.common import handle_slash_command

from .support import *  # noqa: F403


def session_payload(path: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        decode_session_record(path.read_text(encoding="utf-8")).model_dump(mode="json"),
    )


def test_session_store_migrates_legacy_session_stream(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
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
    path = tmp_path / "legacy-stream.jsonl"
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

    loaded = store.load("legacy-stream")

    assert loaded.version == 5
    assert loaded.title == "Legacy stream"
    assert [message.plain_text_content for message in loaded.messages] == [
        "legacy hello"
    ]
    assert path.read_text(encoding="utf-8").startswith(
        '{"type":"yoke_session","version":2}\n'
    )
    assert store.load("legacy-stream") == loaded


def test_session_store_does_not_rewrite_unsupported_legacy_schema(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    path = tmp_path / "too-old.jsonl"
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

    with pytest.raises(ValueError, match="Unsupported session schema version: 3"):
        store.load("too-old")

    assert path.read_text(encoding="utf-8") == raw_text


def test_cli_session_json_keeps_transcript_after_compaction(
    tmp_path: Path, monkeypatch
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

    exit_code = run_cli(
        CLIArgs(
            prompt="follow-up",
            headless=True,
            session="compact-demo",
            root=str(tmp_path),
        ),
        agent=agent,
    )

    assert exit_code == 0
    record = store.load("compact-demo")
    assert any(
        "older answer alpha" in (message.content or "") for message in record.messages
    )
    assert any(entry.kind == "memory_snapshot" for entry in record.conversation_entries)
    payload = session_payload(session_dir / "compact-demo.jsonl")
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
    agent = FakeAgent(provider=provider)
    messages = [
        Message.user("The automatic title is stale."),
        Message.assistant("I will inspect it."),
    ]
    active_session = create_active_session(CLIArgs(root=str(tmp_path)), root=tmp_path)
    active_session.title = "Old title"
    console_stream = CaptureStream()

    handled, returned_messages, updated_session = handle_slash_command(
        "/regenerate-title",
        agent=agent,
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


def test_cli_auto_persists_unnamed_session_globally(tmp_path: Path, capsys) -> None:
    agent = FakeAgent()

    exit_code = run_cli(
        CLIArgs(prompt="persist this", headless=True, root=str(tmp_path)),
        agent=agent,
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
    path = tmp_path / "emoji-demo.jsonl"

    write_session_record(
        record,
        path=path,
    )

    raw_text = path.read_text(encoding="utf-8")
    decoded = decode_session_record(raw_text)

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


def test_cli_uses_local_title_for_first_prompt(tmp_path: Path, capsys) -> None:
    agent = FakeAgent()

    exit_code = run_cli(
        CLIArgs(
            prompt="please fix the config loader bug",
            headless=True,
            root=str(tmp_path),
        ),
        agent=agent,
    )

    assert exit_code == 0
    capsys.readouterr()
    records = SessionStore().list(root=tmp_path)
    assert records[0].title == "please fix the config loader bug"


def test_session_load_reconciles_newer_index_title(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save(
        "title-demo",
        [Message.user("hello")],
        root=tmp_path,
        title="Generated title",
    )
    index = store._load_index()
    index.sessions["title-demo"].title = "Manual title"
    index.sessions["title-demo"].updated_at = "2999-01-01T00:00:00+00:00"
    store._index_path().write_text(index.model_dump_json(indent=2), encoding="utf-8")

    loaded = store.load("title-demo")
    reloaded = store.load("title-demo")

    assert loaded.title == "Manual title"
    assert reloaded.title == "Manual title"


def test_title_slash_command_persists_jsonl_title(tmp_path: Path, monkeypatch) -> None:
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
    active_session = create_active_session(
        CLIArgs(session="title-command", root=str(tmp_path)),
        root=tmp_path,
    )

    handled, _messages, _session = handle_slash_command(
        "/title Manual title",
        agent=FakeAgent(),
        active_session=active_session,
        messages=messages,
        console=build_console(CaptureStream()),
    )
    record = decode_session_record(
        (session_dir / "title-command.jsonl").read_text(encoding="utf-8")
    )

    assert handled is True
    assert record.title == "Manual title"


def test_pin_slash_command_toggles_active_session(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    store.save("pin-demo", [Message.user("hello")], root=tmp_path)
    active_session = create_active_session(
        CLIArgs(session="pin-demo", root=str(tmp_path)),
        root=tmp_path,
    )
    console_stream = CaptureStream()
    console = build_console(console_stream)

    handled, messages, updated_session = handle_slash_command(
        "/pin",
        agent=FakeAgent(),
        active_session=active_session,
        messages=active_session.record.messages,
        console=console,
    )

    assert handled is True
    assert messages == active_session.record.messages
    assert updated_session.record.pinned is True
    assert store.load("pin-demo").pinned is True
    assert "Session pinned: pin-demo" in console_stream.getvalue()


def test_pin_slash_command_persists_new_session(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    active_session = create_active_session(
        CLIArgs(root=str(tmp_path)),
        root=tmp_path,
    )

    handled, _messages, updated_session = handle_slash_command(
        "/pin",
        agent=FakeAgent(),
        active_session=active_session,
        messages=[],
        console=build_console(CaptureStream()),
    )

    assert handled is True
    assert updated_session.record.pinned is True
    assert SessionStore().load(updated_session.id).pinned is True


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
        model_id="gpt-5.5",
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
    assert forked.model_id == "gpt-5.5"
    assert forked.context_usage is None
    assert (session_dir / "forked.jsonl").exists()


def test_fork_slash_command_switches_to_persisted_copy(
    tmp_path: Path, monkeypatch
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore()
    messages = [Message.user("hello"), Message.assistant("hi")]
    store.save("fork-source", messages, root=tmp_path, title="Fork title")
    active_session = create_active_session(
        CLIArgs(session="fork-source", root=str(tmp_path)),
        root=tmp_path,
    )
    console_stream = CaptureStream()

    handled, forked_messages, forked_session = handle_slash_command(
        "/fork",
        agent=FakeAgent(),
        active_session=active_session,
        messages=messages,
        console=build_console(console_stream),
    )

    forked = SessionStore().load(forked_session.id)
    assert handled is True
    assert forked_session.id != active_session.id
    assert forked_messages == messages
    assert forked.messages == messages
    assert forked.title == "Fork title (fork)"
    assert (session_dir / f"{forked_session.id}.jsonl").exists()
    assert f"Forked session fork-source -> {forked_session.id}" in (
        console_stream.getvalue()
    )


def test_cli_fork_starts_from_source_session(
    tmp_path: Path, monkeypatch, capsys
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
        model_id="gpt-5.5",
    )

    exit_code = run_cli(
        CLIArgs(
            prompt="next",
            headless=True,
            fork_session_id="source-cli",
            root=str(tmp_path),
        ),
        agent=FakeAgent(),
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
    tmp_path: Path, monkeypatch
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
        model_id="gpt-5.5",
        reasoning_effort="low",
        context_window_tokens=300_000,
    )
    active_session = create_active_session(
        CLIArgs(session="info-demo", root=str(tmp_path)),
        root=tmp_path,
    )
    console_stream = CaptureStream()

    handled, _messages, _session = handle_slash_command(
        "/info",
        agent=FakeAgent(),
        active_session=active_session,
        messages=active_session.record.messages,
        console=build_console(console_stream),
    )

    output = console_stream.getvalue()
    assert handled is True
    assert "note Session info:" in output
    assert "Session id: info-demo" in output
    assert "Title: Info demo" in output
    assert "Pinned: no" in output
    assert f"Path: {session_dir / 'info-demo.jsonl'}" in output
    assert "Provider: codex" in output
    assert "Model: gpt-5.5" in output
    assert "Messages: 2" in output
    assert "Conversation entries: 2" in output
    assert "Reasoning effort: low" in output
    assert "Context window: 300000 tokens" in output
