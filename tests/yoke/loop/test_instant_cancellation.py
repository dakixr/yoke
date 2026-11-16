from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101
import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import yoke.agent.loop.in_process_tool as in_process_tool_module
import yoke.agent.loop.tool_process as tool_process_module
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.in_process_tool import (
    InProcessToolShutdownError,
    execute_in_process_tool,
)
from yoke.agent.loop.tool_process import ToolProcessInvocation, _process_context
from yoke.agent.models import ConversationEntry, Message, ToolCall, ToolFunction
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.tools import LocalTool, ToolRuntimeContext
from yoke.cli.interactive.common import (
    PendingPrompt,
    PromptCliState,
    format_context_usage_text,
)
from yoke.cli.interactive.prompt.control import (
    create_prompt_toolkit_control,
)
from yoke.cli.interactive.prompt.loop import (
    process_prompt_toolkit_prompt,
)
from yoke.cli.interactive.prompt.scrollback import ScrollbackKind
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.tool_inspector import ToolTraceStore
from yoke.cli.render import build_console
from yoke.cli.runtime.base import ActiveSessionRef
from yoke.mcp.client import McpClientError, StdioMcpClient
from yoke.mcp.config import McpServerConfig
from yoke.mcp.http_client import StreamableHttpClient
from yoke.session.interrupt import active_branch_entries, interrupted_turn_snapshot

from ..cli.terminal_daemon_support import terminal as terminal, session_for
from tests.yoke.daemon.support import eventually


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


def test_queued_skill_waits_for_its_fifo_position(tmp_path, monkeypatch, terminal):
    started, release = threading.Event(), threading.Event()
    provider = ScriptProvider(
        [
            blocked_answer(started, release, "done: first"),
            Message.assistant("done: before"),
            Message.assistant("done: after"),
        ]
    )
    state = PromptCliState(messages=[], pending_prompts=[])
    backend, control = daemon_control(terminal, tmp_path, provider, state)
    skill_root = tmp_path / ".yoke" / "skills" / "example"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: example\ndescription: FIFO skill\n---\nApply the example constraint.\n"
    )
    activations = []
    from yoke.http.runtime.operations import SessionOperations

    activate = SessionOperations.activate_queued_skill

    def activation(self, name):
        activations.append(f"/skill {name}")
        assert len(provider.calls) == 2
        return activate(self, name)

    monkeypatch.setattr(SessionOperations, "activate_queued_skill", activation)
    lock = threading.Lock()
    from yoke.cli.interactive.queue.mutations import append_prompt

    active = session_for(backend)
    ref = ActiveSessionRef(active)
    try:
        worker = control.start_turn("first", None)
        assert started.wait(5)
        assert append_prompt(
            state=state,
            state_lock=lock,
            active_session=active,
            prompt=PendingPrompt("before"),
        )
        state.submit_action = "queue"
        process_prompt_toolkit_prompt(
            "/skill example",
            state=state,
            agent=backend,
            active_session_ref=ref,
            scrollback_console=build_console(io.StringIO()),
            state_lock=lock,
            update_status=lambda _text: None,
            invalidate_prompt=lambda: None,
            request_exit=lambda: None,
            start_turn=control.start_turn,
            steer_active_turn=control.steer_active_turn,
            format_context_usage_text=format_context_usage_text,
        )
        assert append_prompt(
            state=state,
            state_lock=lock,
            active_session=active,
            prompt=PendingPrompt("after"),
        )
        assert activations == []
        assert [item.prompt for item in state.pending_prompts] == [
            "before",
            "/skill example",
            "after",
        ]
        release.set()
        worker.join(5)
        assert (
            backend.connection.session_request(
                "POST", "/wait", params={"timeoutMs": 5000}
            )["data"]["state"]
            == "idle"
        )
        assert activations == ["/skill example"]
        assert [call[-1].text_content() for call in provider.calls] == [
            "first",
            "before",
            "after",
        ]
        assert backend.load_queue().prompts == []
        assert "Apply the example constraint." in repr(provider.calls[-1])
    finally:
        release.set()


class ScriptProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.lock = threading.Lock()

    def fork_for_turn(self):
        return self

    def complete(self, messages, tools):
        del tools
        with self.lock:
            self.calls.append(list(messages))
            step = self.steps.pop(0)
        return step(messages) if callable(step) else step


class ProbeTool(LocalTool):
    name = "probe"
    description = "Return checkpoint work."
    execute_in_process = True

    def execute(self) -> dict[str, object]:
        result = self._context["result"]
        assert isinstance(result, dict)
        return cast(dict[str, object], result)


def daemon_control(
    terminal,
    tmp_path,
    provider,
    state,
    *,
    renderer=None,
    tools=(),
    retire_tool_traces=None,
):
    terminal.factory = lambda _record: RuntimeAgent(provider, tools, tool_root=tmp_path)
    backend = terminal.backend("cancellation", root=tmp_path)
    backend.title("Cancellation fixture", [])
    control = create_prompt_toolkit_control(
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(session_for(backend)),
        renderer=renderer or _renderer(),
        state_lock=threading.Lock(),
        request_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        scrollback=_DiscardScrollback(),
        retire_tool_traces=retire_tool_traces,
    )
    return backend, control


def blocked_answer(started, release, output):
    def complete(_messages):
        started.set()
        assert release.wait(5)
        return Message.assistant(output)

    return complete


def test_steering_starts_replacement_before_retired_turn_finishes(tmp_path, terminal):
    first_started, release_first = threading.Event(), threading.Event()
    second_started, release_second = threading.Event(), threading.Event()
    provider = ScriptProvider(
        [
            blocked_answer(first_started, release_first, "stale result"),
            blocked_answer(second_started, release_second, "accepted result"),
        ]
    )
    state = PromptCliState(messages=[], pending_prompts=[])
    backend, control = daemon_control(terminal, tmp_path, provider, state)
    try:
        retired_worker = control.start_turn("first", None)
        assert first_started.wait(5)
        original_start = state.turn_start_time
        state.turn_tool_count = 3
        assert control.steer_active_turn("second", None)
        assert second_started.wait(5)
        assert not release_first.is_set()
        assert state.turn_start_time == original_start
        assert state.turn_tool_count == 3
        active_worker = state.worker
        assert active_worker is not None
        release_second.set()
        active_worker.join(5)
        assert not active_worker.is_alive()
        release_first.set()
        retired_worker.join(5)
        expected = [
            "first",
            "The previous turn was interrupted by the user before completion. Continue from the current state and follow the user's next instruction.",
            "second",
            "accepted result",
        ]
        assert [message.text_content() for message in state.messages] == expected
        assert [message.text_content() for message in backend.messages()] == expected
    finally:
        release_first.set()
        release_second.set()


def test_instant_stop_emits_total_turn_summary(tmp_path, terminal):
    started, release = threading.Event(), threading.Event()
    summaries = []
    provider = ScriptProvider([blocked_answer(started, release, "late")])
    state = PromptCliState(messages=[], pending_prompts=[])
    backend, control = daemon_control(
        terminal,
        tmp_path,
        provider,
        state,
        renderer=_renderer(emit_turn_summary=summaries.append),
    )
    try:
        worker = control.start_turn("first", None)
        assert started.wait(5)
        state.turn_start_time = time.monotonic() - 5
        state.turn_tool_count = 2
        assert control.stop_active_turn()
        assert len(summaries) == 1
        assert summaries[0]["tool_count"] == 2
        release.set()
        worker.join(5)
        assert "late" not in [message.text_content() for message in backend.messages()]
    finally:
        release.set()


@pytest.mark.parametrize("stop_first", [False, True])
def test_checkpoint_survives_steer_or_stop_and_immediate_continue(
    tmp_path, terminal, stop_first
):
    checkpointed, release_first = threading.Event(), threading.Event()
    second_started, release_second = threading.Event(), threading.Event()
    result = {"valuable": True} if stop_first else {"ok": True}
    output = "continued" if stop_first else "accepted"
    provider = ScriptProvider(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1", function=ToolFunction(name="probe", arguments="{}")
                    )
                ],
            ),
            blocked_answer(checkpointed, release_first, "stale"),
            blocked_answer(second_started, release_second, output),
        ]
    )
    state = PromptCliState(messages=[], pending_prompts=[])
    backend, control = daemon_control(
        terminal, tmp_path, provider, state, tools=[ProbeTool.bind(result=result)]
    )
    try:
        retired_worker = control.start_turn("first", None)
        assert checkpointed.wait(5)
        persisted = terminal.store.load(backend.session.id)
        assert any(
            entry.message is not None and entry.message.role == "tool"
            for entry in persisted.conversation_entries
        )
        if stop_first:
            assert control.stop_active_turn()
            active_worker = control.start_turn("continue", None)
        else:
            assert control.steer_active_turn("second", None)
            active_worker = state.worker
            assert active_worker is not None
        assert second_started.wait(5)
        # The request is the daemon's persisted checkpoint plus its next input.
        replacement_messages = provider.calls[-1][:-1]
        assert [message.role for message in replacement_messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert (
            sum(
                message.role == "user" and message.text_content() == "first"
                for message in replacement_messages
            )
            == 1
        )
        assert (
            replacement_messages[-1]
            .text_content()
            .startswith("The previous turn was interrupted")
        )
        import json

        assert json.loads(replacement_messages[2].text_content()) == result
        before_completion = terminal.store.load(backend.session.id)
        assert any(
            entry.message is not None
            and entry.message.role == "tool"
            and json.loads(entry.message.text_content()) == result
            for entry in before_completion.conversation_entries
        )
        release_second.set()
        active_worker.join(5)
        assert not active_worker.is_alive()
        release_first.set()
        retired_worker.join(5)
        persisted_after = terminal.store.load(backend.session.id)
        persisted_text = [
            entry.message.text_content()
            for entry in persisted_after.conversation_entries
            if entry.message is not None
        ]
        assert output in persisted_text
        assert "stale" not in persisted_text
        assert any(
            entry.message is not None
            and entry.message.role == "tool"
            and json.loads(entry.message.text_content()) == result
            for entry in persisted_after.conversation_entries
        )
    finally:
        release_first.set()
        release_second.set()


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


def test_turn_cancellation_retires_live_tool_traces(tmp_path, terminal):
    started, release = threading.Event(), threading.Event()

    class BlockingTool(LocalTool):
        name = "web_research"
        description = "Wait for physical completion."
        execute_in_process = True

        question: str

        def execute(self) -> dict[str, object]:
            assert self.question == "Spain"
            started.set()
            assert release.wait(5)
            return {"ok": True}

    provider = ScriptProvider(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-cancelled",
                        function=ToolFunction(
                            name="web_research", arguments='{"question":"Spain"}'
                        ),
                    )
                ],
            )
        ]
    )
    state = PromptCliState(messages=[], pending_prompts=[])
    store = ToolTraceStore()
    trace_ready = threading.Event()
    unsubscribe = store.subscribe(trace_ready.set)
    renderer = _renderer()
    renderer._record_tool_event = store.record_event
    backend, control = daemon_control(
        terminal,
        tmp_path,
        provider,
        state,
        renderer=renderer,
        tools=[BlockingTool.bind()],
        retire_tool_traces=store.retire_turn,
    )
    try:
        worker = control.start_turn("search", None)
        assert started.wait(5), (
            [(entry.status, entry.result) for entry in store.snapshot()],
            backend.connection.session_request(
                "POST", "/wait", params={"timeoutMs": 1}
            ),
        )
        assert trace_ready.wait(5)
        assert store.snapshot()[0].status == "running"
        assert control.stop_active_turn()
        cancelled = store.snapshot()[0]
        assert cancelled.status == "cancelled"
        assert cancelled.result == {
            "ok": False,
            "cancelled": True,
            "error": "Tool execution cancelled with its turn.",
        }
        release.set()
        worker.join(5)
        assert store.snapshot()[0].status == "cancelled"
        store.record_event(
            "tool_execution_start",
            {"turn_id": 1, "tool_call_id": "call-late", "tool_name": "late_tool"},
        )
        assert [entry.tool_call_id for entry in store.snapshot()] == ["call-cancelled"]
        eventually(
            lambda: (
                backend.tool_inspection().call("call-cancelled").status == "cancelled"
            )
        )
    finally:
        release.set()
        unsubscribe()


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
    assert started.wait(timeout=5)
    assert stopped is True
    assert result["cancelled"] is True

    closer = threading.Thread(target=agent.close)
    closer.start()
    try:
        assert cancellation_seen.wait(timeout=5)
        assert closer.is_alive()
        assert lifecycle == []
    finally:
        release.set()
        closer.join(timeout=5)

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
    assert started.wait(timeout=5)
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
    # Close must time out instead of waiting for the tool's 5s release.
    assert time.monotonic() - started_at < 2.5
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

    branch = active_branch_entries(entries, leaf_id=parent_id)
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
