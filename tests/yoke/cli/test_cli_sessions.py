from __future__ import annotations

# ruff: noqa: F403,F405,S101,D100,D103,ANN401

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

    forked = store.fork("source", new_session_id_value="forked")

    assert forked.id == "forked"
    assert forked.messages == store.load("source").messages
    assert forked.title == "Source title (fork)"
    assert forked.pinned is False
    assert forked.provider_name == "codex"
    assert forked.model_id == "gpt-5.5"
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
