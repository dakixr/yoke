from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,D100,D103,F405,S101

from threading import Lock

from .support import *  # noqa: F403, F405


def test_prompt_toolkit_stop_persists_interrupted_runtime_state(tmp_path: Path) -> None:
    from yoke.agent.tools import LocalTool
    from yoke.cli.interactive.common import PromptCliState
    from yoke.cli.interactive.prompt_control import create_prompt_toolkit_control

    class BlockingProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="blocking_tool",
                            arguments="{}",
                        ),
                    )
                ],
            )

    class BlockingTool(LocalTool):
        name = "blocking_tool"
        description = "Block until cancellation is requested."
        is_yoke_tool = True

        def execute(self) -> dict[str, object]:
            deadline = time.monotonic() + 2
            while not self._is_cancel_requested():
                if time.monotonic() > deadline:
                    raise AssertionError("tool was not cancelled")
                time.sleep(0.01)
            return {"ok": False, "cancelled": True, "error": "cancelled"}

    state = PromptCliState(messages=[], pending_prompts=[])
    state.abandoned_turn_ids = set()
    state.steered_turn_ids = set()
    active_session = active_session_for(tmp_path)
    active_session_ref = {"active_session": active_session}
    agent = RuntimeAgent(
        provider=BlockingProvider(),
        tools=[BlockingTool.bind()],
    )
    lock = Lock()
    statuses: list[str] = []
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda status: statuses.append(status),
    )
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref=active_session_ref,
        renderer=renderer,
        scrollback_console=build_console(io.StringIO()),
        state_lock=lock,
        estimate_toolbar_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda status: statuses.append(status),
        run_in_scrollback=lambda callback: callback(),
    )

    worker = control.start_turn("please run tool", None)
    deadline = time.monotonic() + 2
    while not any(status == "Running tool" for status in statuses):
        if time.monotonic() > deadline:
            raise AssertionError("turn did not reach tool execution")
        time.sleep(0.01)

    assert control.stop_active_turn() is True
    worker.join(timeout=2)

    assert not worker.is_alive()
    record = active_session.store.load(active_session.id)
    assert [message.role for message in record.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert record.messages[0].content == "please run tool"
    assert record.messages[-1].content == INTERRUPTED_TURN_NOTICE
    assert any(
        entry.kind == "assistant_tool_calls" for entry in record.conversation_entries
    )
    tool_result = json.loads(record.messages[2].text_content() or "{}")
    assert tool_result["cancelled"] is True
    assert [message.role for message in state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
