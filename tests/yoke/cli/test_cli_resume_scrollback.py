from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002, ANN003, ANN401, D100, D103, F401, F403, F405, S101

from .support import *  # noqa: F403, F405
from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import Compactor
from yoke.agent.prompting import MEMORY_MESSAGE_PREFIX


def test_interactive_cli_reports_tool_failures(tmp_path: Path) -> None:
    class FailingToolAgent:
        supports_message_history = True
        supports_user_message = False

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del stop_requested
            if on_event is not None:
                on_event("model_start", {"iteration": 1})
                on_event(
                    "tool_execution_start",
                    {
                        "tool_name": COMMAND_TOOL_NAME,
                        "tool_arguments": '{"command":"false"}',
                    },
                )
                on_event(
                    "tool_execution_end",
                    {
                        "tool_name": COMMAND_TOOL_NAME,
                        "ok": False,
                        "result": {"error": "command exited with status 1"},
                    },
                )
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            conversation.append(Message.assistant("recovered"))
            return AgentResult(output="recovered", messages=conversation, iterations=1)

    prompts = iter(["test tool failure", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()

    exit_code = run_cli(
        CLIArgs(root=str(tmp_path)),
        agent=FailingToolAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert f'{COMMAND_TOOL_NAME} command="false"' in stdout.getvalue()
    assert "command exited with status 1" in stdout.getvalue()
    assert "recovered" in stdout.getvalue()


def test_interactive_cli_persists_partial_messages_on_provider_error(
    tmp_path: Path,
) -> None:
    class PartialFailureAgent:
        supports_message_history = True
        supports_user_message = False

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del on_event, stop_requested
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            conversation.append(
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name=COMMAND_TOOL_NAME,
                                arguments='{"command":"write file"}',
                            ),
                        )
                    ],
                )
            )
            conversation.append(
                Message.tool(
                    tool_call_id="call-1",
                    content='{"ok": true, "output": "wrote file"}',
                )
            )
            raise ProviderError("provider unavailable", partial_messages=conversation)

    prompts = iter(["do side effect", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()

    exit_code = run_cli(
        CLIArgs(root=str(tmp_path)),
        agent=PartialFailureAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    records = SessionStore().list(root=tmp_path)
    assert exit_code == 0
    assert "provider unavailable" in stdout.getvalue()
    assert len(records) == 1
    saved = SessionStore().load(records[0].id)
    assert [message.role for message in saved.messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert saved.messages[-1].content == '{"ok": true, "output": "wrote file"}'


def test_resume_by_id_continues_saved_session(tmp_path: Path) -> None:
    store = SessionStore()
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


def test_resume_by_id_uses_persisted_compaction_handoff(
    tmp_path: Path,
) -> None:
    class RecordingProvider(Provider):
        def __init__(self) -> None:
            self.provider_texts: list[str] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.provider_texts.append(
                "\n".join(message.text_content() or "" for message in messages)
            )
            return Message.assistant("resumed with handoff")

    context_manager = ContextManager()
    context = context_manager.initialize(
        "recent",
        [Message.user("older"), Message.assistant("older answer")],
    )
    preparation = CompactionPreparation(
        reason="manual",
        estimate=Compactor().estimate_tokens(
            context.messages,
            reserve_tokens=0,
        ),
        boundary="user",
        messages_to_summarize=context.messages,
        kept_messages=[Message.user("recent")],
        recent_user_messages=[Message.user("recent")],
    )
    context_manager.apply_compaction(
        context,
        preparation,
        summary_text="resume handoff summary",
    )
    store = SessionStore()
    store.save(
        "compacted",
        context_manager.transcript_messages(context),
        conversation_entries=context.conversation_log.entries,
        root=tmp_path,
        title="Compacted session",
    )
    provider = RecordingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(),
    )
    prompts = iter(["next", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "compacted",
        agent=agent,
        input_func=fake_input,
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert provider.provider_texts
    assert MEMORY_MESSAGE_PREFIX in provider.provider_texts[-1]
    assert "resume handoff summary" in provider.provider_texts[-1]
    saved = store.load("compacted")
    memory_entries = [
        entry for entry in saved.conversation_entries if entry.kind == "memory_snapshot"
    ]
    assert memory_entries
    handoff = cast(dict[str, object], memory_entries[-1].metadata["compaction_handoff"])
    assert handoff["summary_text"] == "resume handoff summary"


def test_resume_replays_tool_calls_to_scrollback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.save(
        "with-tools",
        [
            Message.user("old"),
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name=COMMAND_TOOL_NAME,
                            arguments=json.dumps(
                                {"command": "false", "timeout_seconds": 1}
                            ),
                        ),
                    )
                ],
            ),
            Message.tool(
                "call-1",
                json.dumps({"ok": False, "error": "command exited with status 1"}),
            ),
            Message.assistant("recovered"),
        ],
        root=tmp_path,
        title="Tool session",
    )
    prompts = iter(["quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "with-tools",
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert "user old" in output
    assert f'{COMMAND_TOOL_NAME} command="false" timeout_seconds=1' in output
    assert "command exited with status 1" in output
    assert "recovered" in output


def test_resume_replays_commentary_before_tool_calls_under_tool_divider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.save(
        "commentary-first",
        [
            Message.user("old"),
            Message(
                role="assistant",
                content="Checking the command result.",
                phase="commentary",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name=COMMAND_TOOL_NAME,
                            arguments=json.dumps({"command": "false"}),
                        ),
                    )
                ],
            ),
            Message.tool(
                "call-1",
                json.dumps({"ok": False, "error": "command exited with status 1"}),
            ),
            Message.assistant("recovered"),
        ],
        root=tmp_path,
        title="Commentary-first tool session",
    )
    prompts = iter(["quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "commentary-first",
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert "user old" in output
    assert output.index("Checking the command result.") < output.index(
        f'{COMMAND_TOOL_NAME} command="false"'
    )
    assert "command exited with status 1" in output
    assert "recovered" in output
