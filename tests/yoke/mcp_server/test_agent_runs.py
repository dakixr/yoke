"""Read-only SDK agent snapshots through the real MCP adapter and transport."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import textwrap
from unittest.mock import Mock

import jsonschema
from jsonschema.validators import validator_for
import pytest

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


class Registry:
    def __init__(self, runs: list[dict[str, object]]) -> None:
        self.runs = runs
        self.reads = 0

    def snapshots(self) -> list[dict[str, object]]:
        self.reads += 1
        return self.runs

    def close(self) -> None:
        pass


def run(run_id: str, **values: object) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "agentId": "stable-agent",
        "runId": run_id,
        "sessionID": None,
        "parentRunId": None,
        "parentAgentId": None,
        "runtimeSessionID": 123,
        "name": "reviewer",
        "provider": "fake",
        "model": "test",
        "status": "running",
        "observation": "live",
        "startedAt": "2026-09-28T00:00:00+00:00",
        "finishedAt": None,
        "lastSeenAt": "2026-09-28T00:00:00+00:00",
        "updatedAt": "2026-09-28T00:00:00+00:00",
        "version": 1,
        "lastToolName": None,
        "errorType": None,
        **values,
    }


def test_agent_runs_publishes_typed_schema_annotations_and_empty_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        registry = Registry([])
        monkeypatch.setattr(
            service.runtime.manager, "agent_runs", registry, raising=False
        )
        async with memory_client(service) as client:
            spec = next(
                tool
                for tool in (await client.list_tools()).tools
                if tool.name == "agent_runs"
            )
            annotations = spec.annotations
            assert annotations is not None
            assert annotations.read_only_hint and annotations.idempotent_hint
            assert not annotations.destructive_hint and not annotations.open_world_hint
            assert spec.input_schema["additionalProperties"] is False
            assert set(spec.input_schema["properties"]) == {
                "session_id",
                "active_only",
                "limit",
            }
            assert spec.input_schema["properties"]["limit"]["maximum"] == 200
            assert spec.output_schema is not None
            for schema in (spec.input_schema, spec.output_schema):
                validator_for(schema).check_schema(schema)
            snapshot_schema = spec.output_schema["$defs"]["AgentRunSnapshot"]
            assert snapshot_schema["properties"]["status"]["enum"] == [
                "running",
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            ]
            assert spec.description is not None
            assert "shared" in spec.description and "ChatGPT" in spec.description
            result = await client.call_tool("agent_runs", {})
            assert not result.is_error
            payload = structured(result)
            assert payload == {
                "ok": True,
                "data": [],
                "total": 0,
                "truncated": False,
                "limit": 50,
                "session_id": None,
                "active_only": False,
            }
            jsonschema.validate(payload, spec.output_schema)
            assert registry.reads == 1

    asyncio.run(scenario())


def test_agent_runs_filters_process_and_live_status_before_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        registry = Registry(
            [
                run("old", status="completed"),
                run("stale", observation="stale"),
                run("lost", observation="lost"),
                run("other-process", runtimeSessionID=456),
                run("live", startedAt="2026-09-28T01:00:00+00:00"),
                run("unbound", runtimeSessionID=None),
            ]
        )
        before = deepcopy(registry.runs)
        monkeypatch.setattr(
            service.runtime.manager, "agent_runs", registry, raising=False
        )
        forbidden = Mock(side_effect=AssertionError("Not a registry snapshot read"))
        for name in (
            "exec_command",
            "exec_argv",
            "snapshots",
            "output_chunks",
            "observe_output",
            "write_input",
            "terminate",
            "subscribe",
        ):
            monkeypatch.setattr(service.runtime.manager, name, forbidden)
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool("agent_runs", {"session_id": 123, "limit": 2})
            )
            assert result["total"] == 4 and result["truncated"] is True
            assert len(result["data"]) == 2 and result["data"][0]["runId"] == "live"
            live = structured(
                await service.adapter.call_tool(
                    "agent_runs", {"session_id": 123, "active_only": True, "limit": 1}
                )
            )
            assert live["total"] == 1 and live["truncated"] is False
            assert [item["runId"] for item in live["data"]] == ["live"]
            all_runs = structured(await client.call_tool("agent_runs", {}))
            assert all_runs["total"] == 6
            assert all(item["sessionID"] is None for item in all_runs["data"])
            assert {item["observation"] for item in all_runs["data"]} == {
                "live",
                "stale",
                "lost",
            }
            missing = structured(
                await client.call_tool("agent_runs", {"session_id": 999})
            )
            assert (
                missing["data"] == []
                and missing["total"] == 0
                and not missing["truncated"]
            )
            assert registry.runs == before and registry.reads == 4
            forbidden.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"session_id": True},
        {"session_id": "123"},
        {"session_id": 1.0},
        {"session_id": 0},
        {"session_id": -1},
        {"active_only": "true"},
        {"active_only": 1},
        {"active_only": None},
        {"limit": True},
        {"limit": "1"},
        {"limit": 1.0},
        {"limit": 0},
        {"limit": -1},
        {"limit": 201},
        {"limit": None},
        {"cwd": "/private"},
        {"cursor": "ignored"},
    ],
)
def test_agent_runs_strict_arguments_do_not_read_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        registry = Registry([])
        monkeypatch.setattr(
            service.runtime.manager, "agent_runs", registry, raising=False
        )
        async with memory_client(service) as client:
            for result in (
                await client.call_tool("agent_runs", arguments),
                await service.adapter.call_tool("agent_runs", arguments),
            ):
                assert result.is_error
                payload = structured(result)
                assert payload["error_code"] == "INVALID_ARGUMENT"
                assert payload["execution_started"] is False
            assert registry.reads == 0

    asyncio.run(scenario())


def test_agent_runs_projects_only_public_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        registry = Registry(
            [
                run(
                    "safe",
                    typedUsage={
                        "toolUses": 2,
                        "totalTokens": 40,
                        "durationMs": 1.5,
                        "secret": "private-usage",
                    },
                    prompt="private-prompt",
                    output="private-output",
                    command="private-command",
                    environment={"TOKEN": "private-token"},
                    toolArguments={"key": "private-key"},
                )
            ]
        )
        monkeypatch.setattr(
            service.runtime.manager, "agent_runs", registry, raising=False
        )
        async with memory_client(service) as client:
            result = structured(await client.call_tool("agent_runs", {}))
            assert "private-" not in json.dumps(result)
            assert result["data"][0]["typedUsage"] == {
                "toolUses": 2,
                "totalTokens": 40,
                "durationMs": 1.5,
            }
            assert "taskId" not in result["data"][0]

    asyncio.run(scenario())


def test_agent_runs_never_consumes_command_output_or_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        registry = Registry([])
        monkeypatch.setattr(
            service.runtime.manager.agent_runs, "snapshots", registry.snapshots
        )
        async with memory_client(service) as client:
            started = structured(
                await client.call_tool(
                    "command_exec",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "print('retained-output-' * 1000)",
                        ],
                        "max_output_tokens": 32,
                    },
                )
            )
            assert started["ok"], started
            session_id, cursor = started["session_id"], started["cursor"]
            registry.runs = [run("observed", runtimeSessionID=session_id)]
            request = {
                "sessions": [{"session_id": session_id, "cursor": cursor}],
                "wait_ms": 5000,
            }
            before = structured(await client.call_tool("process_read", request))
            for _ in range(3):
                observed = structured(
                    await client.call_tool("agent_runs", {"session_id": session_id})
                )
                assert observed["total"] == 1
                assert "retained-output" not in json.dumps(observed)
            after = structured(await client.call_tool("process_read", request))
            for field in ("output", "cursor", "has_more_output", "exit_code"):
                assert after["items"][0][field] == before["items"][0][field]
            assert "retained-output" in after["items"][0]["output"]

    asyncio.run(scenario())


def test_agent_runs_bounds_results_without_losing_total_or_terminal_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        states = ("completed", "failed", "cancelled", "interrupted")
        registry = Registry(
            [run(str(index), status=states[index % 4]) for index in range(205)]
        )
        monkeypatch.setattr(
            service.runtime.manager, "agent_runs", registry, raising=False
        )
        async with memory_client(service) as client:
            for arguments, count in (({}, 50), ({"limit": 200}, 200)):
                result = structured(await client.call_tool("agent_runs", arguments))
                assert result["total"] == 205 and result["truncated"] is True
                assert len(result["data"]) == count
                assert {item["status"] for item in result["data"]} == set(states)
            inactive = structured(
                await client.call_tool("agent_runs", {"active_only": True})
            )
            assert inactive["total"] == 0 and not inactive["truncated"]
            assert inactive["data"] == []

    asyncio.run(scenario())


SDK_PROGRAM = textwrap.dedent("""\
    import threading
    from types import SimpleNamespace
    from yoke.ai import Agent, RunConfig
    from yoke.ai.providers.base import Provider
    from yoke.agent.models import Message, ToolCall, ToolFunction
    from yoke.agent.tools import WorkspaceTool

    release = threading.Event()
    results = []

    class Inspect(WorkspaceTool):
        name = "inspect"
        description = "Return a synthetic result."
        execute_in_process = True

        def execute(self):
            return {"ok": True, "output": "private-tool-output"}

    class FakeProvider(Provider):
        config = SimpleNamespace(model="fake-model")

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return Message(role="assistant", tool_calls=[ToolCall(
                    id="inspect-call", type="function",
                    function=ToolFunction(name="inspect", arguments="{}"),
                )])
            assert release.wait(15), "Test did not release provider"
            return Message.assistant("private-provider-output")

    agents = [Agent(provider=FakeProvider(), config=RunConfig(
        root=".", name=name, tools=[Inspect], include_agents_file=False,
        root_session_id="not-a-chatgpt-conversation",
    )) for name in ("alpha", "beta")]

    def prompt(agent):
        results.append(agent.prompt("private-prompt").output)

    threads = [threading.Thread(target=prompt, args=(agent,)) for agent in agents]
    for thread in threads:
        thread.start()
    input()
    release.set()
    for thread in threads:
        thread.join()
    results.append(agents[0].prompt("private-reuse-prompt").output)
    for agent in agents:
        agent.close()
    assert len(results) == 3
    print("private-process-output", flush=True)
""")


@pytest.mark.parametrize("tool_name", ["command_exec", "python_exec"])
def test_managed_sdk_runs_register_before_any_query_and_are_shared_by_clients(
    tmp_path: Path, tool_name: str
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            arguments: dict[str, object] = {"mode": "background"}
            if tool_name == "command_exec":
                arguments["argv"] = [sys.executable, "-c", SDK_PROGRAM]
            else:
                arguments["code"] = SDK_PROGRAM
            started = structured(await client.call_tool(tool_name, arguments))
            assert started["ok"], started
            session_id = started["session_id"]

            # Neither an agent_runs request nor a process output read activates
            # tracking. Both launch tools must use the manager's core registry.
            async with asyncio.timeout(10):
                while True:
                    snapshots = service.runtime.manager.agent_runs.snapshots()
                    if len(snapshots) == 2 and all(
                        item["lastToolName"] == "inspect" for item in snapshots
                    ):
                        break
                    await asyncio.sleep(0.02)
            assert {item["runtimeSessionID"] for item in snapshots} == {session_id}
            assert all(item["sessionID"] is None for item in snapshots)
            async with memory_client(service) as second_client:
                request = {"session_id": session_id, "active_only": True}
                first = structured(await client.call_tool("agent_runs", request))
                second = structured(
                    await second_client.call_tool("agent_runs", request)
                )
                assert first == second
                assert first["total"] == 2 and not first["truncated"]
                assert {item["name"] for item in first["data"]} == {"alpha", "beta"}
                assert all(item["model"] == "fake-model" for item in first["data"])
                assert "private-" not in json.dumps(first)

                sent = structured(
                    await client.call_tool(
                        "process_input",
                        {"session_id": session_id, "chars": "\n", "wait_ms": 1},
                    )
                )
                assert sent["ok"], sent
                finished = structured(
                    await client.call_tool(
                        "process_read",
                        {"sessions": [{"session_id": session_id}], "wait_ms": 10000},
                    )
                )
                assert finished["items"][0]["exit_code"] == 0, finished
                final = structured(
                    await service.adapter.call_tool(
                        "agent_runs", {"session_id": session_id}
                    )
                )
                assert final["total"] == 3 and not final["truncated"]
                assert len({item["runId"] for item in final["data"]}) == 3
                assert len({item["agentId"] for item in final["data"]}) == 2
                assert {item["status"] for item in final["data"]} == {"completed"}
                assert "private-" not in json.dumps(final)
                assert "not-a-chatgpt-conversation" not in json.dumps(final)
                live = structured(await client.call_tool("agent_runs", request))
                assert live["total"] == 0 and live["data"] == []

    asyncio.run(scenario())
