from __future__ import annotations

# ruff: noqa: ANN202, D100, D101, D102, D103, S101

import base64
import io
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import cast

from yoke.agent.loop import AgentResult
from yoke.agent.loop import MessageHistory
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.forking import promote_runtime_fork
from yoke.agent.loop.tools.in_process import execute_in_process_tool
from yoke.agent.loop.tools.process import ToolProcessInvocation
from yoke.agent.loop.tools.process import wait_for_tool_process
from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRuntimeContext
from yoke.agent.tools.image_generation import ImageGenerationTool
from yoke.ai.providers.base import ProviderCancelledError
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.render import build_console

from ..cli.support import active_session_for


def renderer() -> PromptToolkitLiveRenderer:
    return PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda _status: None,
    )


def test_steering_starts_next_generation_under_100ms(tmp_path: Path) -> None:
    @dataclass
    class NonCooperativeAgent:
        supports_message_history = True
        supports_user_message = False
        first_started: threading.Event = field(default_factory=threading.Event)
        release_first: threading.Event = field(default_factory=threading.Event)
        second_started: threading.Event = field(default_factory=threading.Event)

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
            if prompt == "first":
                self.first_started.set()
                self.release_first.wait(timeout=5)
                output = "stale result"
            else:
                self.second_started.set()
                output = "steered result"
            conversation.append(Message.assistant(output))
            return AgentResult(output=output, messages=conversation, iterations=1)

    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    agent = NonCooperativeAgent()
    active_session = active_session_for(tmp_path)
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session},
        renderer=renderer(),
        scrollback_console=build_console(io.StringIO()),
        state_lock=threading.Lock(),
        estimate_toolbar_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        run_in_scrollback=lambda callback: callback(),
    )

    retired_worker = control.start_turn("first", None)
    assert agent.first_started.wait(timeout=1)
    started_at = time.monotonic()
    assert control.steer_active_turn("second", None) is True
    assert agent.second_started.wait(timeout=0.1)
    assert time.monotonic() - started_at < 0.1
    active_worker = state.worker
    assert active_worker is not None
    active_worker.join(timeout=1)
    agent.release_first.set()
    retired_worker.join(timeout=1)

    assert [message.text_content() for message in state.messages] == [
        "first",
        "The previous turn was interrupted by the user before completion. Continue "
        "from the current state and follow the user's next instruction.",
        "second",
        "steered result",
    ]


def test_steering_preserves_completed_active_turn_tool_context(
    tmp_path: Path,
) -> None:
    @dataclass
    class SharedProviderState:
        waiting_after_tool: threading.Event = field(default_factory=threading.Event)
        second_messages: list[Message] = field(default_factory=list)

    class SteeringProvider:
        supports_image_inputs = False
        max_images_per_message = None

        def __init__(self, shared: SharedProviderState) -> None:
            self.shared = shared

        def fork_for_turn(self):
            return SteeringProvider(self.shared)

        def complete(self, messages, tools):
            del messages, tools
            raise AssertionError("expected cancellable completion path")

        def complete_with_cancel(
            self,
            messages,
            tools,
            *,
            cancel_requested,
        ):
            del tools
            user_prompts = [
                message.text_content() for message in messages if message.role == "user"
            ]
            current_prompt = user_prompts[-1] if user_prompts else ""
            if current_prompt == "first":
                if messages[-1].role == "user":
                    return Message(
                        role="assistant",
                        content="I found the CV and inspected it.",
                        tool_calls=[
                            ToolCall(
                                id="inspect-1",
                                function=ToolFunction(
                                    name="inspect_cv",
                                    arguments="{}",
                                ),
                            )
                        ],
                    )
                self.shared.waiting_after_tool.set()
                while not cancel_requested():
                    time.sleep(0.005)
                raise ProviderCancelledError()
            if current_prompt == "second":
                self.shared.second_messages = [
                    message.model_copy(deep=True) for message in messages
                ]
                return Message.assistant("continued from inspection")
            return Message.assistant("Steering context test")

    class InspectCvTool(LocalTool):
        name = "inspect_cv"
        description = "Inspect the test CV."

        def execute(self) -> dict[str, object]:
            return {"ok": True, "path": "daniel_cv.md", "content": "CV details"}

        def apply_result(self, context, result) -> None:
            del result
            context.active_skills = [
                ActiveSkill(
                    name="frontend-design",
                    description="Build polished interfaces.",
                    source_path="<inline>",
                    content="Preserve this design workflow while steering.",
                )
            ]

    shared = SharedProviderState()
    agent = RuntimeAgent(
        provider=cast(Any, SteeringProvider(shared)),
        tools=[InspectCvTool.bind()],
    )
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    active_session = active_session_for(tmp_path)
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session},
        renderer=renderer(),
        scrollback_console=build_console(io.StringIO()),
        state_lock=threading.Lock(),
        estimate_toolbar_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        run_in_scrollback=lambda callback: callback(),
    )

    retired_worker = control.start_turn("first", None)
    assert shared.waiting_after_tool.wait(timeout=2)
    assert control.steer_active_turn("second", None) is True
    active_worker = state.worker
    assert active_worker is not None
    active_worker.join(timeout=2)
    retired_worker.join(timeout=2)

    assert not active_worker.is_alive()
    assert not retired_worker.is_alive()
    assert [
        message.role for message in shared.second_messages if message.role != "system"
    ] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    transcript = [
        message for message in shared.second_messages if message.role != "system"
    ]
    assert transcript[1].text_content() == ("I found the CV and inspected it.")
    assert "daniel_cv.md" in (transcript[2].text_content() or "")
    assert transcript[3].text_content() == (
        "The previous turn was interrupted by the user before completion. Continue "
        "from the current state and follow the user's next instruction."
    )
    assert any(
        "Preserve this design workflow while steering."
        in (message.text_content() or "")
        for message in shared.second_messages
        if message.role == "system"
    )
    assert [skill.name for skill in agent.active_skills] == ["frontend-design"]
    assert [message.text_content() for message in state.messages][-2:] == [
        "second",
        "continued from inspection",
    ]


def test_non_cooperative_process_cancels_under_100ms() -> None:
    class BlockingTool(LocalTool):
        name = "blocking"
        description = "Block forever."

        def execute(self) -> dict[str, object]:
            time.sleep(30)
            return {"ok": True}

    invocation = ToolProcessInvocation(
        tools={"blocking": BlockingTool.bind()},
        name="blocking",
        arguments={},
    )
    invocation.start()
    started_at = time.monotonic()
    result, stopped = wait_for_tool_process(
        invocation,
        stop_requested=lambda: True,
    )

    assert time.monotonic() - started_at < 0.1
    assert stopped is True
    assert result["cancelled"] is True


def test_non_cooperative_in_process_tool_cancels_under_100ms() -> None:
    release = threading.Event()

    class BlockingTool(LocalTool):
        name = "blocking"
        description = "Block without polling cancellation."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            release.wait(timeout=5)
            return {"ok": True}

    started_at = time.monotonic()
    result, stopped = execute_in_process_tool(
        tools={"blocking": BlockingTool.bind()},
        name="blocking",
        arguments={},
        stop_requested=lambda: True,
        tool_event=None,
    )
    elapsed = time.monotonic() - started_at
    release.set()

    assert elapsed < 0.1
    assert stopped is True
    assert result["cancelled"] is True


def test_accepted_turn_promotes_isolated_runtime_state() -> None:
    class Provider:
        supports_image_inputs = False
        max_images_per_message = None

        def complete(self, messages, tools):
            del messages, tools
            return Message.assistant("done")

    original_provider = Provider()
    completed_provider = Provider()
    primary = RuntimeAgent(provider=cast(Any, original_provider), tools=[])
    completed = RuntimeAgent(provider=cast(Any, completed_provider), tools=[])
    completed.load_conversation(
        MessageHistory([Message.user("steer"), Message.assistant("accepted")])
    )

    promote_runtime_fork(primary, completed)

    assert primary.provider is completed_provider
    assert completed.provider is original_provider
    assert [message.text_content() for message in primary.messages] == [
        "steer",
        "accepted",
    ]


def test_cancelled_image_generation_never_publishes_output(tmp_path: Path) -> None:
    class BlockingImageProvider:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def generate_image(self, *, prompt: str) -> str:
            del prompt
            self.started.set()
            self.release.wait(timeout=5)
            self.finished.set()
            return base64.b64encode(b"image").decode()

    provider = BlockingImageProvider()
    stop_event = threading.Event()
    tool = ImageGenerationTool.bind()
    tool.bind_runtime_context(
        ToolRuntimeContext(
            root=tmp_path,
            home=tmp_path,
            provider=cast(Any, provider),
            model=ModelIdentity(provider_name="test", model_id="image"),
        )
    )
    tools = {tool.name: tool}

    def stop_after_start() -> None:
        assert provider.started.wait(timeout=1)
        stop_event.set()

    threading.Thread(target=stop_after_start, daemon=True).start()
    result, stopped = execute_in_process_tool(
        tools=tools,
        name=tool.name,
        arguments={"prompt": "draw", "output_path": "result.png"},
        stop_requested=stop_event.is_set,
        tool_event=None,
    )
    provider.release.set()
    assert provider.finished.wait(timeout=1)

    assert stopped is True
    assert result["cancelled"] is True
    assert not (tmp_path / "result.png").exists()
