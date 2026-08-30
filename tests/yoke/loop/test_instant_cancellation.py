from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

import io
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yoke.agent.loop.in_process_tool as in_process_tool_module
import yoke.agent.loop.tool_process as tool_process_module

from yoke.agent.loop import AgentResult
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.in_process_tool import execute_in_process_tool
from yoke.agent.loop.in_process_tool import InProcessToolShutdownError
from yoke.agent.loop.tool_process import ToolProcessInvocation
from yoke.agent.loop.tool_process import _process_context
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRuntimeContext
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.prompt.cancellation import (
    active_branch_entry_refs,
)
from yoke.cli.interactive.prompt.control import (
    create_prompt_toolkit_control,
)
from yoke.cli.interactive.prompt.loop import (
    process_prompt_toolkit_prompt,
)
from yoke.cli.interactive.prompt.scrollback import ScrollbackKind
from yoke.cli.interactive.prompt.cancellation import (
    interrupted_turn_snapshot,
)
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.tool_inspector import ToolTraceStore
from yoke.cli.render import build_console
from yoke.mcp.client import McpClientError
from yoke.mcp.client import StdioMcpClient
from yoke.mcp.config import McpServerConfig
from yoke.mcp.http_client import StreamableHttpClient

from ..cli.support import active_session_for


class _DiscardScrollback:
    def emit(
        self,
        kind: ScrollbackKind,
        text: str = "",
        *,
        failed: bool = False,
    ) -> None:
        del kind, text, failed


class SlowProcessTool(LocalTool):
    name = "slow_process"
    description = "Block without cooperative cancellation."

    def execute(self) -> dict[str, object]:
        time.sleep(30)
        return {"ok": True}


class ProviderContextTool(LocalTool):
    name = "provider_context"
    description = "Expose the provider bound to this turn."

    def execute(self) -> dict[str, object]:
        return {"provider": self._context["provider"]}


def _renderer(
    *,
    emit_turn_summary: Any = None,
) -> PromptToolkitLiveRenderer:
    return PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda _status: None,
        emit_turn_summary=emit_turn_summary,
    )


def test_queued_skill_waits_for_its_fifo_position(tmp_path, monkeypatch) -> None:
    @dataclass
    class QueueAgent:
        supports_message_history = True
        supports_user_message = False
        first_started: threading.Event = field(default_factory=threading.Event)
        release_first: threading.Event = field(default_factory=threading.Event)
        prompts: list[str] = field(default_factory=list)

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            **_kwargs: Any,
        ) -> AgentResult:
            self.prompts.append(prompt)
            if prompt == "first":
                self.first_started.set()
                self.release_first.wait(timeout=5)
            conversation = [*list(messages or []), Message.user(prompt)]
            conversation.append(Message.assistant(f"done: {prompt}"))
            return AgentResult(
                output=f"done: {prompt}",
                messages=conversation,
                iterations=1,
            )

    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    agent = QueueAgent()
    active_session = active_session_for(tmp_path)
    active_session_ref = {"active_session": active_session}
    state_lock = threading.Lock()
    console = build_console(io.StringIO())
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref=active_session_ref,
        renderer=_renderer(),
        state_lock=state_lock,
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
    )
    activations: list[str] = []

    def activate_skill(command: str, **kwargs: Any) -> tuple[bool, list[Message], Any]:
        activations.append(command)
        return True, kwargs["messages"], kwargs["active_session"]

    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.control.handle_slash_command",
        activate_skill,
    )
    worker = control.start_turn("first", None)
    assert agent.first_started.wait(timeout=1)
    state.pending_prompts.append(PendingPrompt("before"))
    state.submit_action = "queue"

    process_prompt_toolkit_prompt(
        "/skill example",
        state=state,
        agent=agent,
        active_session_ref=active_session_ref,
        scrollback_console=console,
        state_lock=state_lock,
        update_status=lambda _status: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=control.start_turn,
        steer_active_turn=control.steer_active_turn,
        format_context_usage_text=format_context_usage_text,
    )
    state.pending_prompts.append(PendingPrompt("after"))

    assert activations == []
    assert [item.prompt for item in state.pending_prompts] == [
        "before",
        "/skill example",
        "after",
    ]

    agent.release_first.set()
    worker.join(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with state_lock:
            done = (
                state.worker is None
                and not state.pending_prompts
                and len(agent.prompts) == 3
            )
        if done:
            break
        time.sleep(0.01)

    assert activations == ["/skill example"]
    assert agent.prompts == ["first", "before", "after"]
    assert state.pending_prompts == []


def test_steering_starts_replacement_before_retired_turn_finishes(
    tmp_path, monkeypatch
) -> None:
    @dataclass
    class NonCooperativeAgent:
        supports_message_history = True
        supports_user_message = False
        first_started: threading.Event = field(default_factory=threading.Event)
        release_first: threading.Event = field(default_factory=threading.Event)
        second_started: threading.Event = field(default_factory=threading.Event)
        release_second: threading.Event = field(default_factory=threading.Event)

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
                self.release_second.wait(timeout=5)
                output = "accepted result"
            conversation.append(Message.assistant(output))
            return AgentResult(
                output=output,
                messages=conversation,
                iterations=1,
            )

    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    agent = NonCooperativeAgent()
    active_session = active_session_for(tmp_path)
    active_session.title = "Cancellation test"
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session},
        renderer=_renderer(),
        state_lock=threading.Lock(),
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
    )

    retired_worker = control.start_turn("first", None)
    assert agent.first_started.wait(timeout=1)
    original_start = state.turn_start_time
    state.turn_tool_count = 3
    assert control.steer_active_turn("second", None) is True
    assert agent.second_started.wait(timeout=5)
    assert not agent.release_first.is_set()
    assert state.turn_start_time == original_start
    assert state.turn_tool_count == 3

    agent.release_second.set()
    active_worker = state.worker
    assert active_worker is not None
    active_worker.join(timeout=10)
    agent.release_first.set()
    retired_worker.join(timeout=10)

    assert [message.text_content() for message in state.messages] == [
        "first",
        "The previous turn was interrupted by the user before completion. "
        "Continue from the current state and follow the user's next "
        "instruction.",
        "second",
        "accepted result",
    ]


def test_instant_stop_emits_total_turn_summary(tmp_path, monkeypatch) -> None:
    @dataclass
    class BlockingAgent:
        supports_message_history = True
        supports_user_message = False
        started: threading.Event = field(default_factory=threading.Event)
        release: threading.Event = field(default_factory=threading.Event)

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del prompt, messages, on_event, stop_requested
            self.started.set()
            self.release.wait(timeout=5)
            return AgentResult(output="late", messages=[], iterations=1)

    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    summaries: list[dict[str, object]] = []
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    agent = BlockingAgent()
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session_for(tmp_path)},
        renderer=_renderer(emit_turn_summary=summaries.append),
        state_lock=threading.Lock(),
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
    )

    worker = control.start_turn("first", None)
    assert agent.started.wait(timeout=1)
    state.turn_start_time = time.monotonic() - 5
    state.turn_tool_count = 2

    assert control.stop_active_turn() is True
    assert len(summaries) == 1
    assert summaries[0]["tool_count"] == 2

    agent.release.set()
    worker.join(timeout=5)


def test_steering_continues_from_latest_tool_result_checkpoint(
    tmp_path, monkeypatch
) -> None:
    class UnusedAgent:
        supports_message_history = False
        supports_user_message = False

        def run(
            self,
            prompt: str,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del prompt, on_event, stop_requested
            raise AssertionError("execute_turn is replaced in this test")

    checkpointed = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    replacement_messages: list[Message] = []

    def fake_execute_turn(
        _agent,
        prompt,
        messages,
        *,
        after_tool_result_appended=None,
        **_kwargs: Any,
    ) -> AgentResult:
        if prompt == "first":
            checkpoint_messages = [
                *messages,
                Message.user("first"),
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="probe",
                                arguments="{}",
                            ),
                        )
                    ],
                ),
                Message.tool(
                    tool_call_id="call-1",
                    content='{"ok": true}',
                ),
            ]
            checkpoint_entries = conversation_entries_from_messages(checkpoint_messages)
            assert after_tool_result_appended is not None
            after_tool_result_appended(
                checkpoint_messages,
                checkpoint_entries,
            )
            checkpointed.set()
            release_first.wait(timeout=5)
            return AgentResult(
                output="stale",
                messages=[*checkpoint_messages, Message.assistant("stale")],
                conversation_entries=checkpoint_entries,
                iterations=1,
            )
        replacement_messages.extend(messages)
        second_started.set()
        release_second.wait(timeout=5)
        completed_messages = [
            *messages,
            Message.user(prompt),
            Message.assistant("accepted"),
        ]
        return AgentResult(
            output="accepted",
            messages=completed_messages,
            conversation_entries=conversation_entries_from_messages(completed_messages),
            iterations=1,
        )

    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.turns.execute_turn",
        fake_execute_turn,
    )
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    active_session = active_session_for(tmp_path)
    active_session.title = "Checkpoint steering test"
    control = create_prompt_toolkit_control(
        state=state,
        agent=UnusedAgent(),
        active_session_ref={"active_session": active_session},
        renderer=_renderer(),
        state_lock=threading.Lock(),
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
    )

    retired_worker = control.start_turn("first", None)
    assert checkpointed.wait(timeout=1)
    persisted = active_session.store.load(active_session.id)
    assert any(
        entry.message is not None and entry.message.role == "tool"
        for entry in persisted.conversation_entries
    )

    assert control.steer_active_turn("second", None) is True
    assert second_started.wait(timeout=1)
    assert [message.role for message in replacement_messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert (
        sum(
            message.role == "user" and message.plain_text_content == "first"
            for message in replacement_messages
        )
        == 1
    )
    interruption_notice = replacement_messages[-1].plain_text_content
    assert interruption_notice is not None
    assert interruption_notice.startswith("The previous turn was interrupted")

    release_second.set()
    active_worker = state.worker
    assert active_worker is not None
    active_worker.join(timeout=5)
    release_first.set()
    retired_worker.join(timeout=5)
    persisted_after_steer = active_session.store.load(active_session.id)
    persisted_text = [
        entry.message.text_content()
        for entry in persisted_after_steer.conversation_entries
        if entry.message is not None
    ]
    assert '{"ok": true}' in persisted_text
    assert "accepted" in persisted_text
    assert "stale" not in persisted_text


def test_stop_and_immediate_continue_retain_completed_tool_work(
    tmp_path, monkeypatch
) -> None:
    class UnusedAgent:
        supports_message_history = False
        supports_user_message = False

        def run(self, prompt: str, **_kwargs: Any) -> AgentResult:
            del prompt
            raise AssertionError("execute_turn is replaced in this test")

    checkpointed = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    replacement_messages: list[Message] = []
    replacement_entries: list[ConversationEntry] = []

    def fake_execute_turn(
        _agent,
        prompt,
        messages,
        *,
        conversation_entries=None,
        after_tool_result_appended=None,
        **_kwargs: Any,
    ) -> AgentResult:
        if prompt == "first":
            checkpoint_messages = [
                *messages,
                Message.user("first"),
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(name="probe", arguments="{}"),
                        )
                    ],
                ),
                Message.tool("call-1", '{"valuable": true}'),
            ]
            checkpoint_entries = conversation_entries_from_messages(checkpoint_messages)
            assert after_tool_result_appended is not None
            after_tool_result_appended(
                checkpoint_messages,
                checkpoint_entries,
            )
            checkpointed.set()
            release_first.wait(timeout=5)
            return AgentResult(
                output="stale",
                messages=[*checkpoint_messages, Message.assistant("stale")],
                conversation_entries=checkpoint_entries,
                iterations=1,
            )
        replacement_messages.extend(messages)
        replacement_entries.extend(conversation_entries or [])
        second_started.set()
        return AgentResult(
            output="continued",
            messages=[
                *messages,
                Message.user(prompt),
                Message.assistant("continued"),
            ],
            conversation_entries=conversation_entries_from_messages(
                [
                    *messages,
                    Message.user(prompt),
                    Message.assistant("continued"),
                ]
            ),
            iterations=1,
        )

    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.turns.execute_turn",
        fake_execute_turn,
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
        agent=UnusedAgent(),
        active_session_ref={"active_session": active_session},
        renderer=_renderer(),
        state_lock=threading.Lock(),
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
    )

    retired_worker = control.start_turn("first", None)
    assert checkpointed.wait(timeout=1)
    assert control.stop_active_turn() is True
    active_worker = control.start_turn("continue", None)
    assert second_started.wait(timeout=1)

    assert any(
        message.role == "tool" and "valuable" in (message.content or "")
        for message in replacement_messages
    )
    assert any(
        entry.message is not None
        and entry.message.role == "tool"
        and "valuable" in (entry.message.content or "")
        for entry in replacement_entries
    )
    assert replacement_messages[-1].plain_text_content is not None
    assert replacement_messages[-1].plain_text_content.startswith(
        "The previous turn was interrupted"
    )
    persisted = active_session.store.load(active_session.id)
    assert any(
        entry.message is not None
        and entry.message.role == "tool"
        and "valuable" in (entry.message.content or "")
        for entry in persisted.conversation_entries
    )

    active_worker.join(timeout=5)
    release_first.set()
    retired_worker.join(timeout=5)


def test_tool_processes_use_spawn_context() -> None:
    assert _process_context().get_start_method() == "spawn"


def test_tool_process_start_failure_releases_resources(monkeypatch) -> None:
    invocation = ToolProcessInvocation(
        tools={"slow_process": SlowProcessTool.bind()},
        name="slow_process",
        arguments={},
    )

    def fail_start() -> None:
        raise OSError("spawn failed")

    monkeypatch.setattr(invocation._process, "start", fail_start)

    with pytest.raises(OSError, match="spawn failed"):
        invocation.start()

    assert invocation._closed is True
    assert invocation not in list(tool_process_module._ACTIVE_INVOCATIONS)


def test_turn_cancellation_retires_live_tool_traces(tmp_path, monkeypatch) -> None:
    @dataclass
    class ToolEventAgent:
        supports_message_history = True
        supports_user_message = False
        started: threading.Event = field(default_factory=threading.Event)
        release: threading.Event = field(default_factory=threading.Event)

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del stop_requested
            on_event(
                "tool_execution_start",
                {
                    "tool_call_id": "call-cancelled",
                    "tool_name": "web_research",
                    "tool_arguments": '{"question":"Spain"}',
                },
            )
            self.started.set()
            self.release.wait(timeout=5)
            on_event(
                "tool_execution_end",
                {
                    "tool_call_id": "call-cancelled",
                    "tool_name": "web_research",
                    "ok": True,
                    "result": {"ok": True},
                },
            )
            conversation = list(messages or [])
            conversation.extend([Message.user(prompt), Message.assistant("late")])
            return AgentResult(output="late", messages=conversation, iterations=1)

    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    agent = ToolEventAgent()
    store = ToolTraceStore()
    renderer = _renderer()
    renderer._record_tool_event = store.record_event
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session_for(tmp_path)},
        renderer=renderer,
        state_lock=threading.Lock(),
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
        retire_tool_traces=store.retire_turn,
    )

    worker = control.start_turn("search", None)
    assert agent.started.wait(timeout=1)
    assert control.stop_active_turn() is True
    cancelled = store.snapshot()[0]
    assert cancelled.status == "cancelled"
    assert cancelled.result == {
        "ok": False,
        "cancelled": True,
        "error": "Tool execution cancelled with its turn.",
    }

    agent.release.set()
    worker.join(timeout=5)
    assert store.snapshot()[0].status == "cancelled"
    store.record_event(
        "tool_execution_start",
        {
            "turn_id": 1,
            "tool_call_id": "call-late",
            "tool_name": "late_tool",
        },
    )
    assert [entry.tool_call_id for entry in store.snapshot()] == ["call-cancelled"]


def test_non_cooperative_in_process_tool_detaches_during_cancellation() -> None:
    release = threading.Event()

    class SlowInProcessTool(LocalTool):
        name = "slow_in_process"
        description = "Block without cooperative cancellation."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            release.wait(timeout=5)
            return {"ok": True}

    result, stopped = execute_in_process_tool(
        tools={"slow_in_process": SlowInProcessTool.bind()},
        name="slow_in_process",
        arguments={},
        stop_requested=lambda: True,
    )
    release.set()

    assert stopped is True
    assert result["cancelled"] is True


def test_runtime_close_waits_for_cancelled_in_process_tool_before_resources_close(
    tmp_path,
) -> None:
    started = threading.Event()
    cancellation_seen = threading.Event()
    release = threading.Event()
    lifecycle: list[str] = []

    class Resource:
        def close(self) -> None:
            lifecycle.append("resource-closed")

    class CooperativeTool(LocalTool):
        name = "cooperative"
        description = "Wait for cancellation before finishing."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            started.set()
            while not self._is_cancel_requested():
                time.sleep(0.001)
            cancellation_seen.set()
            release.wait()
            lifecycle.append("tool-finished")
            return {"ok": True}

        def owned_resources(self) -> tuple[object, ...]:
            return (self._context["resource"],)

    class Provider:
        supports_image_inputs = False
        max_images_per_message = None

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            return Message.assistant("done")

    agent = RuntimeAgent(
        provider=Provider(),
        tools=[CooperativeTool.bind(resource=Resource())],
        tool_root=tmp_path,
    )
    result, stopped = execute_in_process_tool(
        tools=agent.tools,
        name="cooperative",
        arguments={},
        stop_requested=lambda: True,
    )
    assert started.wait(timeout=1)
    assert stopped is True
    assert result["cancelled"] is True

    closer = threading.Thread(target=agent.close)
    closer.start()
    try:
        assert cancellation_seen.wait(timeout=1)
        assert closer.is_alive()
        assert lifecycle == []
    finally:
        release.set()
        closer.join(timeout=1)

    assert not closer.is_alive()
    assert lifecycle == ["tool-finished", "resource-closed"]


def test_runtime_close_times_out_without_releasing_live_tool_resources(
    tmp_path,
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    resource_closed = threading.Event()

    class Resource:
        def close(self) -> None:
            resource_closed.set()

    class NonCooperativeTool(LocalTool):
        name = "non_cooperative"
        description = "Ignore cancellation until explicitly released."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            started.set()
            release.wait(timeout=5)
            return {"ok": True}

        def owned_resources(self) -> tuple[object, ...]:
            return (self._context["resource"],)

    class Provider:
        supports_image_inputs = False
        max_images_per_message = None

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            return Message.assistant("done")

    agent = RuntimeAgent(
        provider=Provider(),
        tools=[NonCooperativeTool.bind(resource=Resource())],
        tool_root=tmp_path,
    )
    result, stopped = execute_in_process_tool(
        tools=agent.tools,
        name="non_cooperative",
        arguments={},
        stop_requested=lambda: True,
    )
    assert started.wait(timeout=1)
    assert stopped is True
    assert result["cancelled"] is True
    monkeypatch.setattr(
        in_process_tool_module,
        "IN_PROCESS_TOOL_SHUTDOWN_SECONDS",
        0.01,
    )

    started_at = time.monotonic()
    with pytest.raises(InProcessToolShutdownError, match="runtime remains open"):
        agent.close()
    assert time.monotonic() - started_at < 0.5
    assert agent._closed is False
    assert not resource_closed.is_set()

    release.set()
    agent.close()
    assert agent._closed is True
    assert resource_closed.is_set()


def test_runtime_fork_rebinds_tool_provider_to_isolated_provider() -> None:
    class Provider:
        supports_image_inputs = False
        max_images_per_message = None

        def complete(self, messages, tools) -> Message:
            del messages, tools
            return Message.assistant("done")

        def fork_for_turn(self) -> Provider:
            return Provider()

    primary_provider = Provider()
    primary = RuntimeAgent(
        provider=primary_provider,
        tools=[ProviderContextTool.bind()],
    )

    forked = primary.fork(isolate_provider=True)
    tool = forked.tools[ProviderContextTool.name]
    runtime_context = tool._context["runtime_context"]

    assert isinstance(runtime_context, ToolRuntimeContext)
    assert forked.provider is not primary_provider
    assert tool._context["provider"] is forked.provider
    assert runtime_context.provider is forked.provider
    primary.close()
    forked.close()


def test_interruption_closes_incomplete_tool_batch_before_continuation() -> None:
    messages = [
        Message.user("work"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolFunction(name="probe", arguments="{}"),
                ),
                ToolCall(
                    id="call-2",
                    function=ToolFunction(name="probe", arguments="{}"),
                ),
            ],
        ),
        Message.tool("call-1", '{"ok":true}'),
    ]
    entries = conversation_entries_from_messages(messages)

    snapshot_messages, snapshot_entries = interrupted_turn_snapshot(
        messages=messages,
        entries=entries,
        user_message=Message.user("continue"),
        leaf_id=entries[-1].id,
    )

    assert [message.role for message in snapshot_messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
        "assistant",
    ]
    assert snapshot_messages[3].tool_call_id == "call-2"
    assert "cancelled" in (snapshot_messages[3].text_content() or "")
    assert snapshot_entries[3].metadata["recovered_incomplete_tool_call"] is True
    assert snapshot_entries[4].parent_id == snapshot_entries[3].id


def test_large_interruption_checkpoint_reuses_immutable_history() -> None:
    entries: list[ConversationEntry] = []
    messages: list[Message] = []
    parent_id: str | None = None
    for _ in range(10_000):
        message = Message.user("history")
        entry = ConversationEntry(
            kind="user",
            message=message,
            parent_id=parent_id,
        )
        entries.append(entry)
        messages.append(message)
        parent_id = entry.id

    branch = active_branch_entry_refs(entries, leaf_id=parent_id)
    snapshot_messages, snapshot_entries = interrupted_turn_snapshot(
        messages=messages,
        entries=branch,
        user_message=Message.user("active"),
    )

    assert len(snapshot_messages) == 10_002
    assert len(snapshot_entries) == 10_002
    assert snapshot_entries[-3] is entries[-1]


def test_stdio_mcp_request_cancels_and_ignores_late_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = StdioMcpClient(
        McpServerConfig(name="test", command="unused"),
        root=tmp_path,
    )
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(client, "_send", sent.append)

    with pytest.raises(McpClientError, match="cancelled"):
        client.request(
            "tools/call",
            timeout=30,
            cancel_requested=lambda: True,
        )

    client._handle_message({"jsonrpc": "2.0", "id": 1, "result": {"stale": True}})

    def answer_current(payload: dict[str, Any]) -> None:
        sent.append(payload)
        request_id = payload.get("id")
        if isinstance(request_id, int):
            client._handle_message(
                {"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}}
            )

    monkeypatch.setattr(client, "_send", answer_current)

    assert client.request("ping", timeout=1) == {"ok": True}
    assert sent[1] == {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1},
    }


def test_streamable_http_mcp_closes_blocked_sse_on_cancellation(
    tmp_path: Path,
) -> None:
    class BlockingStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.closed = threading.Event()

        def __iter__(self) -> Iterator[bytes]:
            self.closed.wait(timeout=5)
            yield b""

        def close(self) -> None:
            self.closed.set()

    stream = BlockingStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = StreamableHttpClient(
        McpServerConfig(
            name="test",
            transport="streamable-http",
            url="https://mcp.test",
        ),
        root=tmp_path,
        http_client=http_client,
    )

    with pytest.raises(McpClientError, match="cancelled"):
        client.request(
            "tools/call",
            timeout=30,
            cancel_requested=lambda: True,
        )
    http_client.close()

    assert stream.closed.is_set()
