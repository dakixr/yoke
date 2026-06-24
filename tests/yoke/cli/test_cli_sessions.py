from __future__ import annotations

from typing import cast

# ruff: noqa: F403, F405
from .support import *  # noqa: F403, F405


def _entry_message(entry: dict[str, object]) -> dict[str, object]:
    message = entry["message"]
    assert isinstance(message, dict)
    return cast(dict[str, object], message)


def _entry_metadata(entry: dict[str, object]) -> dict[str, object]:
    metadata = entry["metadata"]
    assert isinstance(metadata, dict)
    return cast(dict[str, object], metadata)


def _compaction_handoff(entry: dict[str, object]) -> dict[str, object]:
    handoff = _entry_metadata(entry)["compaction_handoff"]
    assert isinstance(handoff, dict)
    return cast(dict[str, object], handoff)


def _jsonl_conversation_entries(path: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "conversation_entry":
            entries.append(payload["entry"])
    return entries


def test_cli_session_jsonl_keeps_transcript_after_compaction(
    tmp_path: Path, monkeypatch
) -> None:
    class CompactingProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[0].content == COMPACTION_SUMMARY_PROMPT:
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
    entries = _jsonl_conversation_entries(session_dir / "compact-demo.jsonl")
    assert any(
        "older answer alpha" in str(_entry_message(entry).get("content") or "")
        for entry in entries
        if entry.get("message") is not None
    )
    assert all("messages" not in entry for entry in entries)
    assert any(entry["kind"] == "memory_snapshot" for entry in entries)
    memory_entries = [entry for entry in entries if entry["kind"] == "memory_snapshot"]
    handoff = _compaction_handoff(memory_entries[-1])
    assert handoff["summary_text"] == "summary of older work"
    assert handoff["reason"] == "threshold"
    assert handoff["retained_messages"]


def test_cli_persists_compaction_handoff_after_provider_error(
    tmp_path: Path, monkeypatch
) -> None:
    class FailingAfterCompactionProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[0].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("handoff before failure")
            if "Create a concise title" in (messages[0].content or ""):
                return Message.assistant("failure title")
            raise ProviderError("synthetic provider failure")

    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(session_dir))
    agent = RuntimeAgent(
        provider=FailingAfterCompactionProvider(),
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
            prompt="new request " + ("alpha " * 500),
            headless=True,
            session="failed-compact",
            root=str(tmp_path),
        ),
        agent=agent,
    )

    assert exit_code == 1
    entries = _jsonl_conversation_entries(session_dir / "failed-compact.jsonl")
    memory_entries = [entry for entry in entries if entry["kind"] == "memory_snapshot"]
    assert memory_entries
    handoff = _compaction_handoff(memory_entries[-1])
    assert handoff["summary_text"] == "handoff before failure"


def test_session_store_list_prunes_expired_sessions(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)
    now = datetime.now(UTC)
    expired = (now - timedelta(days=31)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()

    old_payload = {
        "id": "old-session",
        "messages": [],
        "created_at": expired,
        "updated_at": expired,
        "root": str(tmp_path.resolve()),
        "title": "Old session",
    }
    fresh_payload = {
        "id": "fresh-session",
        "messages": [],
        "created_at": recent,
        "updated_at": recent,
        "root": str(tmp_path.resolve()),
        "title": "Fresh session",
    }
    (tmp_path / "old-session.json").write_text(
        json.dumps(old_payload, indent=2), encoding="utf-8"
    )
    (tmp_path / "fresh-session.json").write_text(
        json.dumps(fresh_payload, indent=2), encoding="utf-8"
    )
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "sessions": {
                    "old-session": {
                        "id": "old-session",
                        "root": str(tmp_path.resolve()),
                        "title": "Old session",
                        "created_at": expired,
                        "updated_at": expired,
                    },
                    "fresh-session": {
                        "id": "fresh-session",
                        "root": str(tmp_path.resolve()),
                        "title": "Fresh session",
                        "created_at": recent,
                        "updated_at": recent,
                    },
                    "missing-session": {
                        "id": "missing-session",
                        "root": str(tmp_path.resolve()),
                        "title": "Missing session",
                        "created_at": recent,
                        "updated_at": recent,
                    },
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    records = store.list(root=tmp_path)

    assert [record.id for record in records] == ["fresh-session"]
    assert not (tmp_path / "old-session.json").exists()
    assert (tmp_path / "fresh-session.jsonl").exists()
    assert not (tmp_path / "fresh-session.json").exists()
    index_payload = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert sorted(index_payload["sessions"]) == ["fresh-session"]


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


def test_cli_uses_provider_to_title_completed_first_turn(
    tmp_path: Path, capsys
) -> None:
    provider = TitleProvider("Fix config loading")
    agent = FakeAgent()
    agent.provider = provider

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
    assert records[0].title == "Fix config loading"
    assert provider.prompts == ["synthetic response"]


def test_typer_entrypoint_invokes_cli() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "--root" in result.stdout


def test_interactive_cli_accepts_queued_prompts_while_busy(
    tmp_path: Path,
) -> None:
    @dataclass
    class SlowAgent:
        supports_message_history = True
        supports_user_message = False

        seen_history_lengths: list[int] = field(default_factory=list)

        def run(
            self,
            prompt: str,
            messages: Sequence[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del stop_requested
            self.seen_history_lengths.append(len(messages or []))
            if on_event is not None:
                on_event("model_start", {"iteration": 1})
                on_event(
                    "tool_execution_start",
                    {
                        "tool_name": COMMAND_TOOL_NAME,
                        "tool_arguments": '{"command":"sleep 0.1"}',
                    },
                )
            time.sleep(0.1)
            if on_event is not None:
                on_event(
                    "tool_execution_end",
                    {"tool_name": COMMAND_TOOL_NAME, "ok": True},
                )
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            conversation.append(Message.assistant(f"done {prompt}"))
            return AgentResult(
                output=f"done {prompt}", messages=conversation, iterations=1
            )

    prompts = iter(["first", "second", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    agent = SlowAgent()

    exit_code = run_cli(
        CLIArgs(root=str(tmp_path)),
        agent=agent,
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "done first" in stdout.getvalue()
    assert "done second" in stdout.getvalue()
    assert agent.seen_history_lengths == [0, 2]
