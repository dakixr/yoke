from __future__ import annotations

# ruff: noqa: F403, F405
from .support import *  # noqa: F403, F405


def test_compaction_keeps_transcript_and_compacts_provider_context(
    tmp_path: Path,
) -> None:
    class CompactingProvider(Provider):
        def __init__(self) -> None:
            self.provider_calls: list[list[Message]] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[0].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("summary of older work")
            self.provider_calls.append(
                [message.model_copy(deep=True) for message in messages]
            )
            return Message.assistant("done")

    provider = CompactingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=tools(tmp_path),
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                max_total_tokens=300,
                keep_recent_tokens=80,
            ),
        ),
        messages=[
            Message.user("older"),
            Message.assistant("older answer " + ("alpha " * 200)),
            Message.user("recent"),
            Message.assistant("recent answer"),
        ],
    )

    result = agent.run("follow-up")

    assert result.output == "done"
    assert any(
        "older answer alpha" in (message.content or "") for message in result.messages
    )
    assert result.conversation_entries is not None
    assert any(entry.kind == "memory_snapshot" for entry in result.conversation_entries)
    provider_text = "\n".join(
        message.text_content() or "" for message in provider.provider_calls[-1]
    )
    assert "summary of older work" in provider_text
    assert "older answer alpha" not in provider_text


def test_agent_reset_clears_owned_state(tmp_path: Path) -> None:
    class EchoProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            return Message.assistant(messages[-1].text_content() or "")

    agent = RuntimeAgent(provider=EchoProvider(), tools=tools(tmp_path))

    first = agent.run("alpha")
    agent.reset()
    second = agent.run("beta")

    assert [message.content for message in first.messages] == ["alpha", "alpha"]
    assert [message.content for message in second.messages] == ["beta", "beta"]


def test_agent_loop_supports_parallel_tool_execution_and_hooks(
    tmp_path: Path,
) -> None:
    before_seen: list[dict[str, object]] = []
    after_seen: list[dict[str, object]] = []
    events: list[str] = []
    event_payloads: list[tuple[str, dict[str, object]]] = []
    barrier = multiprocessing.Barrier(2, timeout=5)
    agent = RuntimeAgent(
        provider=ParallelProvider(tool_name="barrier"),
        tools=[BarrierTool.bind(barrier=barrier)],
    )

    def record_before(ctx: BeforeToolCallContext) -> None:
        before_seen.append(ctx.arguments)
        return None

    def record_after(ctx: AfterToolCallContext) -> None:
        after_seen.append(ctx.result)
        return None

    result = agent.run(
        "run in parallel",
        on_event=lambda event, payload: (
            events.append(event),
            event_payloads.append((event, payload)),
        ),
        before_tool_call=record_before,
        after_tool_call=record_after,
    )

    assert result.output == "done"
    assert len(before_seen) == 2
    assert len(after_seen) == 2
    assert "model_start" in events
    assert "tool_execution_start" in events
    assert "tool_execution_end" in events
    tool_events = [
        (event, payload.get("tool_call_id"))
        for event, payload in event_payloads
        if event in {"tool_execution_start", "tool_execution_end"}
    ]
    first_end_index = next(
        index for index, (event, _) in enumerate(tool_events) if event == "tool_execution_end"
    )
    started_before_first_end = {
        tool_call_id for event, tool_call_id in tool_events[:first_end_index] if event == "tool_execution_start"
    }
    assert started_before_first_end == {"call-1", "call-2"}


def test_agent_loop_stops_after_provider_boundary(tmp_path: Path) -> None:
    stop_event = Event()
    agent = RuntimeAgent(provider=StoppingProvider(stop_event), tools=tools(tmp_path))

    result = agent.run("stop now", stop_requested=stop_event.is_set)

    assert result.status == "stopped"
    assert result.output == "Stopped current turn."
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert result.messages[-1].content == INTERRUPTED_TURN_NOTICE


def test_agent_stop_preserves_tool_turn_for_steering(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")
    stop_event = Event()
    provider = StoppableToolProvider()
    agent = RuntimeAgent(provider=provider, tools=tools(tmp_path))

    def request_stop(_: BeforeToolCallContext) -> None:
        stop_event.set()
        return None

    stopped = agent.run(
        "Inspect notes",
        stop_requested=stop_event.is_set,
        before_tool_call=request_stop,
    )

    assert stopped.status == "stopped"
    assert [message.role for message in stopped.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    cancelled_result = json.loads(stopped.messages[2].text_content() or "{}")
    assert cancelled_result["cancelled"] is True
    assert stopped.messages[-1].content == INTERRUPTED_TURN_NOTICE

    continued = agent.run("Use config.py instead")

    assert continued.status == "completed"
    assert continued.output == "corrected"


def test_agent_stop_terminates_non_cooperative_tool_process(tmp_path: Path) -> None:
    class SleepProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def complete(self, messages: list[Message], tools: list[dict[str, object]]) -> Message:
            del messages, tools
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(name="sleep_forever", arguments="{}"),
                    )
                ],
            )

    class SleepForeverTool(LocalTool):
        name = "sleep_forever"
        description = "Sleep without checking cancellation."

        def execute(self) -> dict[str, object]:
            started = self._context.get("started")
            assert started is not None
            started.set()
            time.sleep(30)
            return {"ok": True}

    stop_event = Event()
    started = multiprocessing.Event()
    agent = RuntimeAgent(
        provider=SleepProvider(),
        tools=[SleepForeverTool.bind(started=started)],
    )

    def request_stop() -> None:
        assert started.wait(timeout=5)
        stop_event.set()

    stopper = Thread(target=request_stop, daemon=True)

    started_at = time.monotonic()
    stopper.start()
    stopped = agent.run(
        "sleep forever",
        stop_requested=stop_event.is_set,
    )
    stopper.join(timeout=1)

    assert time.monotonic() - started_at < 5
    assert stopped.status == "stopped"
    cancelled_result = json.loads(stopped.messages[2].text_content() or "{}")
    assert cancelled_result["cancelled"] is True


def test_tool_process_cancelled_when_wait_is_interrupted(tmp_path: Path) -> None:
    from yoke.agent.loop.tool_process import ToolProcessInvocation
    from yoke.agent.loop.tool_process import wait_for_tool_process

    class SleepForeverTool(LocalTool):
        name = "sleep_forever"
        description = "Sleep without checking cancellation."

        def execute(self) -> dict[str, object]:
            started = self._context.get("started")
            assert started is not None
            started.set()
            time.sleep(30)
            return {"ok": True}

    started = multiprocessing.Event()
    invocation = ToolProcessInvocation(
        tools={"sleep_forever": SleepForeverTool.bind(started=started)},
        name="sleep_forever",
        arguments={},
    )
    invocation.start()

    def interrupt_after_start() -> bool:
        if started.is_set():
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        wait_for_tool_process(invocation, stop_requested=interrupt_after_start)

    assert not invocation._process.is_alive()


def test_active_tool_process_cleanup_cancels_started_invocation(tmp_path: Path) -> None:
    from yoke.agent.loop.tool_process import ToolProcessInvocation
    from yoke.agent.loop.tool_process import cancel_active_tool_processes

    class SleepForeverTool(LocalTool):
        name = "sleep_forever"
        description = "Sleep without checking cancellation."

        def execute(self) -> dict[str, object]:
            started = self._context.get("started")
            assert started is not None
            started.set()
            time.sleep(30)
            return {"ok": True}

    started = multiprocessing.Event()
    invocation = ToolProcessInvocation(
        tools={"sleep_forever": SleepForeverTool.bind(started=started)},
        name="sleep_forever",
        arguments={},
    )
    invocation.start()
    assert started.wait(timeout=5)

    cancel_active_tool_processes()

    assert not invocation._process.is_alive()


def test_agent_stop_terminates_python_exec_process(tmp_path: Path) -> None:
    from yoke.agent.tools import PythonExecTool

    class PythonSleepProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def complete(self, messages: list[Message], tools: list[dict[str, object]]) -> Message:
            del messages, tools
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="python_exec",
                            arguments=json.dumps(
                                {
                                    "code": (
                                        "from pathlib import Path\n"
                                        "import time\n"
                                        "Path('python-started').write_text('started')\n"
                                        "time.sleep(30)"
                                    ),
                                    "timeout": 60,
                                }
                            ),
                        ),
                    )
                ],
            )

    stop_event = Event()
    marker = tmp_path / "python-started"
    agent = RuntimeAgent(
        provider=PythonSleepProvider(),
        tools=[PythonExecTool.bind(root=tmp_path)],
    )

    def request_stop() -> None:
        while not marker.exists():
            if time.monotonic() - started_at > 5:
                raise AssertionError("python_exec process did not start")
            time.sleep(0.01)
        stop_event.set()

    started_at = time.monotonic()
    stopper = Thread(target=request_stop, daemon=True)
    stopper.start()
    stopped = agent.run(
        "sleep in python",
        stop_requested=stop_event.is_set,
    )
    stopper.join(timeout=1)

    assert time.monotonic() - started_at < 5
    assert stopped.status == "stopped"
    cancelled_result = json.loads(stopped.messages[2].text_content() or "{}")
    assert cancelled_result["cancelled"] is True
