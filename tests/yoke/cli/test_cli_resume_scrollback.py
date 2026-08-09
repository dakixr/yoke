from __future__ import annotations

# ruff: noqa: ANN002, ANN003, ANN401, D100, D103, F401, F403, F405, S101

from .support import *  # noqa: F403

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.cli.runtime.resume import RESUME_SCROLLBACK_MESSAGE_LIMIT
from yoke.cli.runtime.resume import project_resumed_session
from yoke.cli.session import SessionRecord


def test_resume_by_id_continues_saved_session(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore(directory=session_dir)
    store.save(
        "saved",
        [Message.user("old"), Message.assistant("answer")],
        root=tmp_path,
        title="Saved session",
    )
    agent = FakeAgent(outputs=["resumed"])
    prompts = iter(["next", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "saved",
        agent=agent,
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert agent.seen_history_lengths == [2]
    output = stdout.getvalue()
    assert "user old" in output
    assert "answer" in output
    assert "resumed" in output
    record = store.load("saved")
    assert [message.role for message in record.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_resume_falls_back_when_saved_provider_is_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore(directory=session_dir)
    store.save(
        "saved",
        [Message.user("old"), Message.assistant("answer")],
        root=tmp_path,
        provider_name="codex-websockets",
        model_id="legacy-model",
    )
    agent = FakeAgent(outputs=["resumed"])
    calls: list[str | None] = []

    def fake_resolve_runtime_agent(args: CLIArgs, *, agent: Any) -> tuple[Any, None]:
        calls.append(args.model)
        if args.model == "codex-websockets:legacy-model":
            raise ValueError(
                "Unsupported provider 'codex-websockets'. Supported providers: demo."
            )
        return agent, None

    monkeypatch.setattr(
        "yoke.cli.runtime.cli._resolve_runtime_agent",
        fake_resolve_runtime_agent,
    )
    prompts = iter(["next", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "saved",
        agent=agent,
        input_func=fake_input,
        stdout=CaptureStream(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == ["codex-websockets:legacy-model", None]
    assert "Falling back to an available provider" in stderr.getvalue()


def test_resume_scrollback_keeps_consecutive_tool_calls_compact() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)
    first_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"one"}'),
    )
    second_call = ToolCall(
        id="call-2",
        function=ToolFunction(name="read", arguments='{"path":"two"}'),
    )

    print_session_scrollback(
        console,
        [
            Message.user("inspect"),
            Message(role="assistant", tool_calls=[first_call]),
            Message(role="assistant", tool_calls=[second_call]),
        ],
    )

    assert 'read path="one"\nread path="two"' in stdout.getvalue()


def test_scrollback_handoff_does_not_hide_earlier_tool_activity() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)
    tool_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"one"}'),
    )

    print_session_scrollback(
        console,
        [
            Message.user("inspect"),
            Message(role="assistant", tool_calls=[tool_call]),
            Message.tool("call-1", '{"ok": true, "output": "contents"}'),
            Message.assistant("Found it."),
            Message.user(render_memory_message("Internal handoff.")),
            Message.user("continue"),
            Message.assistant("Continuing."),
        ],
    )

    output = stdout.getvalue()
    assert "user inspect" in output
    assert 'read path="one"' in output
    assert "Found it." in output
    assert "Internal handoff." not in output
    assert "user continue" in output
    assert "Continuing." in output


def test_large_resume_projects_compact_runtime_and_bounded_scrollback() -> None:
    entries: list[ConversationEntry] = []
    parent_id: str | None = None
    for index in range(10_000):
        entry = ConversationEntry(
            kind="user" if index % 2 == 0 else "assistant",
            message=(
                Message.user(f"prompt {index}")
                if index % 2 == 0
                else Message.assistant(f"answer {index}")
            ),
            parent_id=parent_id,
        )
        entries.append(entry)
        parent_id = entry.id
        if index == 9_900:
            snapshot = ConversationEntry(
                kind="memory_snapshot",
                metadata=MemorySnapshot(
                    id="memory-current",
                    summary_text="Earlier work summarized.",
                ).model_dump(),
                parent_id=parent_id,
            )
            entries.append(snapshot)
            parent_id = snapshot.id
    record = SessionRecord(
        id="large",
        conversation_entries=entries,
        leaf_id=parent_id,
    )

    projection = project_resumed_session(record)

    assert len(projection.runtime_messages) == 5_051
    assert "Earlier work summarized." in (
        projection.runtime_messages[4_951].text_content() or ""
    )
    assert len(projection.scrollback_messages) == RESUME_SCROLLBACK_MESSAGE_LIMIT
    assert projection.scrollback_messages[0].text_content() == "prompt 9600"
    assert projection.scrollback_messages[-1].text_content() == "answer 9999"
    assert projection.scrollback_notice is not None
    assert "9,600 older messages" in projection.scrollback_notice
    assert "compaction summary remains in model context" in (
        projection.scrollback_notice
    )
    assert "Use /tree" in projection.scrollback_notice


def test_resume_scrollback_hides_handoff_without_hiding_audit_messages() -> None:
    tool_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"one"}'),
    )
    messages = [
        Message.user("inspect"),
        Message(role="assistant", content="Checking.", tool_calls=[tool_call]),
        Message.tool("call-1", '{"ok": true, "output": "contents"}'),
        Message.assistant("Found it."),
    ]
    entries: list[ConversationEntry] = []
    parent_id: str | None = None
    for message in messages:
        entry = ConversationEntry(
            kind=(
                "user"
                if message.role == "user"
                else "assistant_tool_calls"
                if message.tool_calls
                else "tool_result"
                if message.role == "tool"
                else "assistant"
            ),
            message=message,
            parent_id=parent_id,
        )
        entries.append(entry)
        parent_id = entry.id
    summary = ConversationEntry(kind="compaction_summary", parent_id=parent_id)
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="Internal handoff."
        ).model_dump(),
    )
    persisted_handoff = ConversationEntry(
        kind="user",
        message=Message.user(render_memory_message("Internal handoff.")),
        parent_id=snapshot.id,
    )
    continuation = ConversationEntry(
        kind="user",
        message=Message.user("continue"),
        parent_id=persisted_handoff.id,
    )
    entries.extend([summary, snapshot, persisted_handoff, continuation])
    record = SessionRecord(
        id="persisted-handoff",
        conversation_entries=entries,
        leaf_id=continuation.id,
    )

    projection = project_resumed_session(record)

    assert projection.scrollback_messages == [*messages, continuation.message]
    assert any(
        "Internal handoff." in (message.text_content() or "")
        for message in projection.runtime_messages
    )


def test_resume_scrollback_reconnects_detached_compaction_handoff() -> None:
    historical_user = ConversationEntry(
        kind="user",
        message=Message.user("inspect"),
    )
    historical_agent = ConversationEntry(
        kind="assistant",
        message=Message.assistant("Found it."),
        parent_id=historical_user.id,
    )
    summary = ConversationEntry(
        kind="compaction_summary",
        parent_id=historical_agent.id,
    )
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current",
            summary_text="Detached handoff.",
        ).model_dump(),
    )
    persisted_handoff = ConversationEntry(
        kind="user",
        message=Message.user(render_memory_message("Detached handoff.")),
    )
    continuation = ConversationEntry(
        kind="user",
        message=Message.user("continue"),
        parent_id=persisted_handoff.id,
    )
    record = SessionRecord(
        id="detached-handoff",
        conversation_entries=[
            historical_user,
            historical_agent,
            summary,
            snapshot,
            persisted_handoff,
            continuation,
        ],
        leaf_id=continuation.id,
    )

    projection = project_resumed_session(record)

    assert projection.scrollback_messages == [
        historical_user.message,
        historical_agent.message,
        continuation.message,
    ]
    assert projection.runtime_messages == [
        persisted_handoff.message,
        continuation.message,
    ]


def test_resume_recovers_checkpoint_bypassed_by_active_leaf() -> None:
    root = ConversationEntry(kind="user", message=Message.user("old"))
    summary = ConversationEntry(kind="compaction_summary", parent_id=root.id)
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="recovered summary"
        ).model_dump(),
    )
    tail = ConversationEntry(
        kind="user", message=Message.user("tail"), parent_id=root.id
    )
    record = SessionRecord(
        id="orphaned-checkpoint",
        conversation_entries=[root, summary, snapshot, tail],
        leaf_id=tail.id,
    )

    projection = project_resumed_session(record)

    assert [entry.kind for entry in projection.active_entries] == [
        "user",
        "user",
    ]
    assert len(projection.runtime_messages) == 2
    assert projection.runtime_messages[0].text_content() == "old"
    assert projection.runtime_messages[1].text_content() == "tail"


def test_resume_does_not_write_unchanged_session_before_interactive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    store = SessionStore(directory=session_dir)
    store.save(
        "saved",
        [Message.user("old"), Message.assistant("answer")],
        root=tmp_path,
    )
    save_calls: list[str] = []
    original_save = SessionStore.save

    def tracked_save(self, session_id, messages, **kwargs) -> SessionRecord:
        save_calls.append(session_id)
        return original_save(self, session_id, messages, **kwargs)

    def fake_interactive(*args, **kwargs) -> int:
        del args
        assert kwargs["replay_session"] is True
        assert [message.text_content() for message in kwargs["replay_messages"]] == [
            "old",
            "answer",
        ]
        return 0

    monkeypatch.setattr(SessionStore, "save", tracked_save)
    monkeypatch.setattr(
        "yoke.cli.interactive.run_interactive_cli",
        fake_interactive,
    )

    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "saved",
        agent=FakeAgent(),
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert save_calls == []


def test_save_reuses_loaded_record_without_decoding_file_again(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(directory=tmp_path)
    store.save("current", [Message.user("hello")], root=tmp_path)
    loaded = store.load("current")
    monkeypatch.setattr(
        "yoke.cli.session.writer.decode_session_record_lines",
        lambda *_args, **_kwargs: pytest.fail("session was decoded again"),
    )

    store.save(
        "current",
        loaded.messages,
        conversation_entries=loaded.conversation_entries,
        leaf_id=loaded.leaf_id,
        root=tmp_path,
        existing_record=loaded,
    )

    assert store.load("current").messages[0].text_content() == "hello"
