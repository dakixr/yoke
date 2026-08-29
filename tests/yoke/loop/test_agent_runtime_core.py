from __future__ import annotations

# ruff: noqa: D100, D103, F403, F405, S101

from yoke.cli.interactive.prompt.turns import retire_turn_agent
from yoke.agent.models import ConversationEntry
from yoke.agent.tools import AttachImageTool
from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)

from .support import *  # noqa: F403


def test_agent_loop_runs_until_final_answer(tmp_path: Path) -> None:
    agent = RuntimeAgent(provider=FakeProvider(), tools=tools(tmp_path))

    result = agent.run("Create a file")

    assert result.output == "done"
    assert result.iterations == 2
    assert (tmp_path / "hello.txt").read_text() == "hello"
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_tool_injected_image_keeps_provider_role_without_becoming_user_history(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "preview.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f"
        b"\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    class AttachProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls += 1
            if self.calls == 1:
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="attach-1",
                            function=ToolFunction(
                                name="attach_image",
                                arguments=json.dumps(
                                    {
                                        "path": str(image_path),
                                        "caption": "Verification image",
                                    }
                                ),
                            ),
                        )
                    ],
                )
            assert [message.role for message in messages] == [
                "user",
                "assistant",
                "tool",
                "user",
            ]
            assert isinstance(messages[-1].content, list)
            assert isinstance(messages[-1].content[0], MessageTextContentPart)
            assert messages[-1].content[0].text == "Verification image"
            return Message.assistant("done")

    agent = RuntimeAgent(
        provider=AttachProvider(),
        tools=[AttachImageTool.bind(root=tmp_path)],
    )
    result = agent.run("Inspect the rendered page")

    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert result.conversation_entries is not None
    assert [entry.kind for entry in result.conversation_entries] == [
        "user",
        "assistant_tool_calls",
        "tool_result",
        "tool_context",
        "assistant",
    ]
    injected = result.conversation_entries[3]
    assert injected.metadata["tool_name"] == "attach_image"
    assert injected.metadata["tool_call_id"] == "attach-1"


def test_runtime_command_process_managers_are_isolated_and_shared_by_forks(
    tmp_path: Path,
) -> None:
    shared_tools = tools(tmp_path)
    primary = RuntimeAgent(provider=FakeProvider(), tools=shared_tools)
    independent = RuntimeAgent(provider=FakeProvider(), tools=shared_tools)
    forked = primary.fork()
    try:
        assert (
            primary.command_process_manager is not independent.command_process_manager
        )
        assert forked.command_process_manager is primary.command_process_manager
        assert (
            primary.tools[COMMAND_TOOL_NAME] is not independent.tools[COMMAND_TOOL_NAME]
        )
        assert (
            primary.tools[COMMAND_TOOL_NAME]._context["command_process_manager"]
            is primary.command_process_manager
        )
        assert (
            independent.tools[COMMAND_TOOL_NAME]._context["command_process_manager"]
            is independent.command_process_manager
        )
        assert (
            forked.tools[COMMAND_TOOL_NAME]._context["command_process_manager"]
            is primary.command_process_manager
        )
    finally:
        forked.close()
        independent.close()
        primary.close()


def test_retired_turn_releases_command_process_manager_lease(
    tmp_path: Path,
) -> None:
    primary = RuntimeAgent(provider=FakeProvider(), tools=tools(tmp_path))
    forked = primary.fork(isolate_provider=True)
    manager = primary.command_process_manager
    try:
        retire_turn_agent(forked, primary_agent=primary)
        deadline = time.monotonic() + 5
        while not forked._closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert forked._closed
    finally:
        forked.close()
        primary.close()

    with pytest.raises(RuntimeError, match="manager is closed"):
        manager.acquire()


def test_runtime_releases_process_manager_when_tool_cleanup_fails(
    tmp_path: Path,
) -> None:
    class RaisingResource:
        def close(self) -> None:
            raise RuntimeError("cleanup failed")

    class ResourceTool(LocalTool):
        name = "resource"
        description = "Own a test resource."

        def execute(self) -> dict[str, object]:
            return {}

        def owned_resources(self) -> tuple[object, ...]:
            return (self._context["resource"],)

    agent = RuntimeAgent(
        provider=FakeProvider(),
        tools=[ResourceTool.bind(resource=RaisingResource())],
        tool_root=tmp_path,
    )
    manager = agent.command_process_manager

    with pytest.raises(RuntimeError, match="cleanup failed"):
        agent.close()
    with pytest.raises(RuntimeError, match="manager is closed"):
        manager.acquire()


def test_runtime_constructor_failure_releases_process_manager(
    tmp_path: Path,
) -> None:
    manager = CommandProcessManager()

    with pytest.raises(ValueError, match="Duplicate tool names"):
        RuntimeAgent(
            provider=FakeProvider(),
            tools=[ReadTool.bind(root=tmp_path), ReadTool.bind(root=tmp_path)],
            command_process_manager=manager,
        )

    with pytest.raises(RuntimeError, match="manager is closed"):
        manager.acquire()


def test_agent_loop_attaches_partial_messages_to_provider_error(
    tmp_path: Path,
) -> None:
    class FailingAfterToolProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls += 1
            if self.calls == 1:
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="fast_write",
                                arguments=(
                                    '{"path":"side_effect.txt","text":"persisted"}'
                                ),
                            ),
                        )
                    ],
                )
            assert messages[-1].role == "tool"
            raise ProviderError("provider unavailable")

    agent = RuntimeAgent(provider=FailingAfterToolProvider(), tools=tools(tmp_path))

    try:
        agent.run("Create a file")
    except ProviderError as exc:
        partial_messages = exc.partial_messages
    else:
        raise AssertionError("Expected provider error")

    assert (tmp_path / "side_effect.txt").read_text() == "persisted"
    assert partial_messages is not None
    assert [message.role for message in partial_messages] == [
        "user",
        "assistant",
        "tool",
    ]


def test_agent_loop_can_continue_existing_history(tmp_path: Path) -> None:
    agent = RuntimeAgent(
        provider=HistoryProvider(),
        tools=tools(tmp_path),
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
        ),
        messages=[
            Message.user("previous task"),
            Message.assistant("previous answer"),
        ],
    )

    result = agent.run("next task")

    assert result.output == "continued"
    assert [message.role for message in result.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_loaded_entries_keep_new_user_prompt_visible_to_provider(
    tmp_path: Path,
) -> None:
    class RecordingProvider(Provider):
        def complete(self, messages, tools):
            del tools
            assert [message.role for message in messages] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            assert messages[-1].plain_text_content == "explore the folder"
            return Message.assistant("working")

    instruction = ConversationEntry(
        id="instruction", kind="instruction", message=Message.system("old")
    )
    user = ConversationEntry(
        id="user",
        parent_id=instruction.id,
        kind="user",
        message=Message.user("hi"),
    )
    assistant = ConversationEntry(
        id="assistant",
        parent_id=user.id,
        kind="assistant",
        message=Message.assistant("hello"),
    )
    agent = RuntimeAgent(
        provider=RecordingProvider(),
        tools=tools(tmp_path),
        context_manager=ContextManager(instructions=[Message.system("current")]),
        conversation_entries=[instruction, user, assistant],
    )

    result = agent.run("explore the folder")

    assert result.output == "working"
    assert result.conversation_entries is not None
    assert all(entry.kind != "instruction" for entry in result.conversation_entries)


def test_loaded_entries_preserve_sibling_branches(tmp_path: Path) -> None:
    class DoneProvider(Provider):
        def complete(self, messages, tools):
            del messages, tools
            return Message.assistant("done")

    root = ConversationEntry(id="root", kind="user", message=Message.user("root"))
    first = ConversationEntry(
        id="first",
        parent_id=root.id,
        kind="assistant",
        message=Message.assistant("first branch"),
    )
    second = ConversationEntry(
        id="second",
        parent_id=root.id,
        kind="assistant",
        message=Message.assistant("second branch"),
    )
    agent = RuntimeAgent(
        provider=DoneProvider(),
        tools=tools(tmp_path),
        conversation_entries=[root, first, second],
    )

    result = agent.run("continue second")

    assert result.conversation_entries is not None
    by_id = {entry.id: entry for entry in result.conversation_entries}
    assert by_id[first.id].parent_id == root.id
    assert by_id[second.id].parent_id == root.id


def test_agent_loop_emits_context_usage_after_tool_results(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    agent = RuntimeAgent(provider=FakeProvider(), tools=tools(tmp_path))

    result = agent.run(
        "Create a file",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    usage_events = [payload for event, payload in events if event == "context_usage"]
    assert result.output == "done"
    assert len(usage_events) == 1
    assert usage_events[0]["reason"] == "tool_results"
    assert usage_events[0]["message_count"] == 3
    assert isinstance(usage_events[0]["input_tokens"], int)


def test_agent_loop_rejects_newest_message_over_provider_image_limit(
    tmp_path: Path,
) -> None:
    image_parts = [
        MessageLocalImageContentPart(
            path=str(tmp_path / f"image-{index}.png"),
            label=f"[Image #{index}]",
        )
        for index in range(1, 52)
    ]
    newest_message = Message.user(
        [MessageTextContentPart(text="Too many images."), *image_parts]
    )
    agent = RuntimeAgent(provider=FakeProvider(), tools=[], messages=[newest_message])

    with pytest.raises(ProviderError, match="exceeds provider image limit"):
        agent.run("", user_message=newest_message)
