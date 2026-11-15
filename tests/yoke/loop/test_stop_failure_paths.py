"""User stop arriving from another thread during compaction and tool calls.

``RuntimeAgent.run`` executes on a worker thread while the test thread plays
the user pressing stop; providers and tools block on events so the stop lands
at a known point without sleeps.
"""

# ruff: noqa: D101, D102, D103, S101
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any


from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.context import CompactionPolicy, ContextManager
from yoke.agent.loop import AgentResult, AgentStoppedError, RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools import LocalTool
from yoke.ai.providers.base import Provider, ProviderCancelledError, ProviderError


class Run:
    """Run the agent on a thread and capture its result or exception."""

    def __init__(self, agent: RuntimeAgent, prompt: str, stop: threading.Event) -> None:
        self.result: AgentResult | None = None
        self.error: BaseException | None = None
        self.finished_at: float | None = None
        self.thread = threading.Thread(
            target=self._run, args=(agent, prompt, stop), daemon=True
        )
        self.thread.start()

    def _run(self, agent: RuntimeAgent, prompt: str, stop: threading.Event) -> None:
        try:
            self.result = agent.run(prompt, stop_requested=stop.is_set)
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            self.error = exc
        finally:
            self.finished_at = time.monotonic()

    def join(self, timeout: float = 5) -> None:
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "agent run did not end after stop"


def _overflowing_agent(provider: Provider) -> RuntimeAgent:
    return RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
            compaction_policy=CompactionPolicy(
                max_total_tokens=800,
                reserved_output_tokens=100,
                keep_recent_tokens=120,
            ),
        ),
        messages=[
            Message.user("older request"),
            Message.assistant("older response " + ("alpha " * 120)),
        ],
    )


class OverflowThenBlockingCompaction(Provider):
    """Overflow once, then block the compaction summary until released."""

    supports_image_inputs = True

    def __init__(self, *, honor_cancel: bool) -> None:
        self.calls: list[str] = []
        self.honor_cancel = honor_cancel
        self.compacting = threading.Event()
        self.release = threading.Event()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        return self._complete(messages, lambda: False)

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Any,
    ) -> Message:
        return self._complete(messages, cancel_requested)

    def _complete(self, messages: list[Message], cancel_requested: Any) -> Message:
        if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
            self.calls.append("compaction")
            self.compacting.set()
            while not self.release.wait(0.01):
                if self.honor_cancel and cancel_requested():
                    raise ProviderCancelledError()
            return Message.assistant("Summarized older context")
        if not self.calls:
            self.calls.append("overflow")
            raise ProviderError("context_length_exceeded: maximum context length")
        self.calls.append("retry")
        return Message.assistant("retried after compaction")


def test_stop_after_blocking_overflow_compaction_never_makes_the_retry_call() -> None:
    """A summary call that ignores cancellation completes; the retry must not run."""
    provider = OverflowThenBlockingCompaction(honor_cancel=False)
    stop = threading.Event()
    run = Run(_overflowing_agent(provider), "newest request", stop)
    try:
        assert provider.compacting.wait(5)
        stop.set()
        provider.release.set()
        run.join()
    finally:
        provider.release.set()

    assert isinstance(run.error, AgentStoppedError), run.error or run.result
    assert provider.calls == ["overflow", "compaction"]


def test_stop_cancels_in_flight_overflow_compaction_summary() -> None:
    provider = OverflowThenBlockingCompaction(honor_cancel=True)
    stop = threading.Event()
    run = Run(_overflowing_agent(provider), "newest request", stop)
    try:
        assert provider.compacting.wait(5)
        stop.set()
        # The provider honors cancellation, so the run must end without release.
        run.join(timeout=2)
    finally:
        provider.release.set()
        run.thread.join(5)

    assert isinstance(run.error, AgentStoppedError), run.error or run.result
    assert provider.calls == ["overflow", "compaction"]


class HangingTool(LocalTool):
    name = "hang"
    description = "Block without honoring cancellation."
    execute_in_process = True

    def execute(self) -> dict[str, object]:
        started, release = self._context["started"], self._context["release"]
        assert isinstance(started, threading.Event)
        assert isinstance(release, threading.Event)
        started.set()
        release.wait(10)
        return {"ok": True, "late": "result from a retired tool"}


class ToolThenAnswer(Provider):
    supports_image_inputs = True

    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if len(self.requests) == 1:
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-hang",
                        function=ToolFunction(name="hang", arguments="{}"),
                    )
                ],
            )
        return Message.assistant("answered")


def _tool_pairs(messages: list[Message]) -> tuple[list[str], list[str]]:
    calls = [call.id for message in messages for call in message.tool_calls or []]
    results = [
        message.tool_call_id or "" for message in messages if message.role == "tool"
    ]
    return calls, results


def test_stop_during_long_tool_call_ends_turn_with_consistent_tool_results(
    tmp_path: Path,
) -> None:
    started, release = threading.Event(), threading.Event()
    provider = ToolThenAnswer()
    agent = RuntimeAgent(
        provider,
        [HangingTool.bind(started=started, release=release)],
        tool_root=tmp_path,
    )
    stop = threading.Event()
    run = Run(agent, "use the tool", stop)
    try:
        assert started.wait(5)
        stopped_at = time.monotonic()
        stop.set()
        run.join()
        assert run.finished_at is not None
        stop_latency = run.finished_at - stopped_at
    finally:
        release.set()

    assert run.error is None, run.error
    result = run.result
    assert result is not None and result.status == "stopped"
    assert stop_latency < 2.0
    assert len(provider.requests) == 1
    calls, results = _tool_pairs(result.messages)
    # Every tool call is closed by exactly one result, and it records the
    # cancellation rather than the hung tool's eventual output.
    assert calls == results == ["call-hang"]
    tool_message = next(m for m in result.messages if m.role == "tool")
    tool_text = tool_message.text_content() or ""
    assert "cancel" in tool_text.lower()
    assert "late" not in tool_text

    # The next turn continues from a provider-valid transcript.
    follow_up = agent.run("continue", stop_requested=lambda: False)
    assert follow_up.status != "stopped"
    assert follow_up.output == "answered"
    sent = provider.requests[-1]
    calls, results = _tool_pairs(sent)
    assert calls == results == ["call-hang"]
    assert sent[-1].role == "user" and "continue" in (sent[-1].plain_text_content or "")
    assert not any(
        "result from a retired tool" in (message.content or "")
        for message in sent
        if isinstance(message.content, str)
    )
