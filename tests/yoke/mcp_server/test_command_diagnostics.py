"""Command-schema recovery through the same HTTP interface as remote clients."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from yoke.agent.tools.command import ExecCommandTool
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.diagnostics import MCPLogFormatter
from yoke.mcp_server.server import create_service

from .helpers import http_client, structured


def test_command_descriptor_has_named_fields_without_a_root_union(
    tmp_path: Path,
) -> None:
    original = ExecCommandTool.model_json_schema(by_alias=True)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with service.server.session_manager.run(), http_client(service) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            command = tools["command_exec"]
            schema = command.input_schema
            assert schema["type"] == "object"
            assert not {"anyOf", "oneOf", "allOf"} & schema.keys()
            assert schema["additionalProperties"] is False
            assert {"cmd", "argv", "command", "workdir"} <= schema["properties"].keys()
            assert '{"cmd":"pwd"}' in (command.description or "")
            assert '{"argv":["pwd"]}' in (command.description or "")
            validator = Draft202012Validator(schema)
            for arguments in ({"cmd": "pwd"}, {"argv": ["pwd"]}, {"command": "pwd"}):
                validator.validate(arguments)
            for arguments in (
                {"cmd": ["pwd"]},
                {"argv": [""]},
                {"cmd": "pwd", "timeout": 4},
            ):
                assert not validator.is_valid(arguments)
            assert schema["properties"]["login"]["default"] is False
            assert command.annotations is not None
            assert command.annotations.destructive_hint is True

    asyncio.run(scenario())
    assert ExecCommandTool.model_json_schema(by_alias=True) == original
    assert "anyOf" in original


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"cmd": ["printf", "secret-command-value"]},
        {"cmd": None},
        {"argv": None},
        {"cmd": ""},
        {"argv": []},
        {"argv": [""]},
        {"cmd": "pwd", "argv": ["pwd"]},
        {"cmd": "pwd", "command": "pwd"},
        {"command": "pwd", "argv": ["pwd"]},
        {"cmd": "pwd", "timeout": 5},
        {"argv": ["pwd"], "shell": "/bin/sh"},
    ],
)
def test_invalid_commands_never_dispatch_and_have_logged_recovery(
    tmp_path: Path,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")
    dispatched: list[object] = []

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))

        async def execute(*args: object) -> dict[str, object]:
            dispatched.append(args)
            raise AssertionError("Invalid arguments must not dispatch")

        monkeypatch.setattr(service.runtime, "execute", execute)
        async with service.server.session_manager.run(), http_client(service) as client:
            result = await client.call_tool("command_exec", arguments)
            payload = structured(result)
            assert result.is_error is True
            assert payload["error_code"] == "INVALID_ARGUMENT"
            assert payload["stage"] == "input_validation"
            assert payload["execution_started"] is False
            assert "No command started" in payload["recovery"]
            assert result.meta is not None
            assert result.meta["yoke/request_id"] == payload["request_id"]
            assert "secret-command-value" not in json.dumps(payload)
            if isinstance(arguments.get("cmd"), list):
                assert (payload["field"], payload["expected"], payload["received"]) == (
                    "cmd",
                    "string",
                    "array",
                )

    asyncio.run(scenario())
    assert dispatched == []
    records = [r for r in caplog.records if hasattr(r, "mcp_event")]
    events = [json.loads(MCPLogFormatter().format(r)) for r in records]
    assert [e["event"] for e in events] == ["tool_call_started", "tool_call_finished"]
    assert events[0]["request_id"] == events[1]["request_id"]
    assert events[1]["error_code"] == "INVALID_ARGUMENT"
    assert events[1]["execution_started"] is False
    assert events[1]["duration_ms"] >= 0
    assert events[1]["version"]
    assert "secret-command-value" not in json.dumps(events)
