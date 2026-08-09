# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

import asyncio
from io import StringIO
import json
from pathlib import Path
from typing import cast

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.tools import WorkspaceTool
from yoke.ai import Agent
from yoke.ai import AgentTraceEvent
from yoke.ai import BatchTask
from yoke.ai import CompositeObserver
from yoke.ai import ConsoleObserver
from yoke.ai import JsonlObserver
from yoke.ai import RunConfig
from yoke.ai import run_many
from yoke.ai.providers.base import Provider
from yoke.ai.sdk.observability import notify_observers
from yoke.ai.sdk.observability import render_trace_event


class ScriptedProvider(Provider):
    def __init__(self, responses: list[Message]) -> None:
        self.responses = responses
        self.index = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        response = self.responses[self.index]
        self.index += 1
        return response


class InspectTool(WorkspaceTool):
    name = "inspect"
    description = "Inspect a path."
    execute_in_process = True
    path: str
    api_token: str

    def execute(self) -> dict[str, object]:
        return {"ok": True, "path": self.path}


def _config(
    tmp_path: Path, *, tools: list[type[InspectTool]] | None = None
) -> RunConfig:
    return RunConfig(
        root=tmp_path,
        tools=tools or [],
        include_agents_file=False,
    )


def test_console_observer_actions_show_messages_tools_and_redaction(
    tmp_path: Path,
) -> None:
    stream = StringIO()
    trace_path = tmp_path / "agent-trace.jsonl"
    observer = CompositeObserver(
        ConsoleObserver("actions", stream=stream, label="worker"),
        JsonlObserver(trace_path, "full"),
    )
    tool_call = ToolCall(
        id="call-1",
        type="function",
        function=ToolFunction(
            name="inspect",
            arguments=json.dumps({"path": "src/app.py", "api_token": "do-not-print"}),
        ),
    )
    provider = ScriptedProvider(
        [
            Message(
                role="assistant",
                content="I will inspect the file.",
                phase="commentary",
                tool_calls=[tool_call],
            ),
            Message.assistant("Inspection complete."),
        ]
    )
    agent = Agent(
        provider=provider,
        config=_config(tmp_path, tools=[InspectTool]),
        observer=observer,
    )

    try:
        result = agent.prompt("Inspect the file.")
    finally:
        agent.close()

    output = stream.getvalue()
    assert result.output == "Inspection complete."
    assert "[worker] I will inspect the file." in output
    assert '[worker] → inspect(path="src/app.py", api_token="<redacted>")' in output
    assert "[worker] Inspection complete." in output
    assert "do-not-print" not in output
    assert "do-not-print" not in trace_path.read_text(encoding="utf-8")


def test_console_observer_messages_hides_tool_calls() -> None:
    stream = StringIO()
    observer = ConsoleObserver("messages", stream=stream)

    observer.observe(
        AgentTraceEvent(
            name="tool_execution_start",
            payload={"tool_name": "read", "tool_arguments": {"path": "a"}},
        )
    )
    observer.observe(
        AgentTraceEvent(
            name="assistant_message",
            payload={"content": "Reading now."},
        )
    )

    assert stream.getvalue() == "Reading now.\n"


def test_jsonl_observer_writes_redacted_structured_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trace.jsonl"
    observer = JsonlObserver(path)

    observer.observe(
        AgentTraceEvent(
            name="tool_execution_start",
            task_id="task-1",
            attempt=2,
            payload={
                "tool_name": "fetch",
                "tool_arguments": json.dumps(
                    {
                        "authorization": "secret-value",
                        "private_key": "private-value",
                    }
                ),
            },
        )
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["event"] == "tool_execution_start"
    assert payload["task_id"] == "task-1"
    assert payload["attempt"] == 2
    arguments = payload["payload"]["tool_arguments"]
    assert arguments["authorization"] == "<redacted>"
    assert arguments["private_key"] == "<redacted>"
    assert "secret-value" not in path.read_text(encoding="utf-8")
    assert "private-value" not in path.read_text(encoding="utf-8")


def test_run_many_observer_labels_concurrent_task_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> str:
        stream = StringIO()
        observer = ConsoleObserver("messages", stream=stream)
        result = await run_many(
            [
                BatchTask(id="alpha", prompt="first"),
                BatchTask(id="beta", prompt="second"),
            ],
            agent_factory=lambda task: Agent(
                provider=ScriptedProvider([Message.assistant(task.id)]),
                config=_config(tmp_path),
            ),
            observer=observer,
        )
        assert result.completed_count == 2
        return stream.getvalue()

    output = asyncio.run(scenario())

    assert "[alpha] alpha" in output
    assert "[beta] beta" in output


def test_observer_failure_does_not_fail_agent(tmp_path: Path, caplog) -> None:
    class BrokenObserver:
        def observe(self, event: AgentTraceEvent) -> None:
            raise OSError(event.name)

    agent = Agent(
        provider=ScriptedProvider([Message.assistant("done")]),
        config=_config(tmp_path),
        observer=BrokenObserver(),
    )

    try:
        result = agent.prompt("continue")
    finally:
        agent.close()

    assert result.output == "done"
    assert "Agent observer failed" in caplog.text


def test_observers_receive_isolated_nested_payloads() -> None:
    source = {"result": {"ok": True, "value": "original"}}
    recorded: list[str] = []

    class MutatingObserver:
        def observe(self, event: AgentTraceEvent) -> None:
            result = cast(dict[str, object], event.payload["result"])
            assert isinstance(result, dict)
            result["value"] = "changed"

    class RecordingObserver:
        def observe(self, event: AgentTraceEvent) -> None:
            result = cast(dict[str, object], event.payload["result"])
            assert isinstance(result, dict)
            recorded.append(str(result["value"]))

    notify_observers(
        (MutatingObserver(), RecordingObserver()),
        "tool_execution_end",
        source,
    )

    assert source["result"]["value"] == "original"
    assert recorded == ["original"]


def test_malformed_tool_arguments_fail_closed() -> None:
    rendered = render_trace_event(
        AgentTraceEvent(
            name="tool_execution_start",
            payload={
                "tool_name": "call",
                "tool_arguments": "authorization=secret-value",
            },
        )
    )

    assert rendered == "→ call(<unavailable>)"


def test_agent_error_respects_max_length() -> None:
    rendered = render_trace_event(
        AgentTraceEvent(name="agent_error", payload={"error": "x" * 100}),
        max_length=20,
    )

    assert rendered is not None
    assert len(rendered) == 20
    assert rendered.endswith("...")


def test_run_many_observes_factory_failures(tmp_path: Path) -> None:
    del tmp_path

    async def scenario() -> str:
        stream = StringIO()

        def fail_factory(task: BatchTask) -> Agent:
            raise RuntimeError(f"cannot build {task.id}")

        result = await run_many(
            [BatchTask(id="broken", prompt="run")],
            agent_factory=fail_factory,
            observer=ConsoleObserver("actions", stream=stream),
        )
        assert result.failed_count == 1
        return stream.getvalue()

    output = asyncio.run(scenario())

    assert "[broken] ! factory: cannot build broken" in output
