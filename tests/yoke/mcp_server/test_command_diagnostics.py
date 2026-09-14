"""Command-schema recovery through the same HTTP interface as remote clients."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
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
            command = tools["exec_command"]
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
            result = await client.call_tool("exec_command", arguments)
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


def test_corrected_retry_runs_once_and_shell_and_alias_still_work(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "runs"
    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('runs'); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
    ]

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with service.server.session_manager.run(), http_client(service) as client:
            rejected = structured(await client.call_tool("exec_command", {"cmd": argv}))
            assert rejected["execution_started"] is False
            assert not marker.exists()
            corrected = structured(
                await client.call_tool("exec_command", {"argv": argv})
            )
            assert corrected["ok"] is True
            assert marker.read_text() == "x"
            for key in ("cmd", "command"):
                result = structured(
                    await client.call_tool("exec_command", {key: "printf shell-ok"})
                )
                assert result["output"] == "shell-ok"
                assert result["exit_code"] == 0

    asyncio.run(scenario())


def test_nonzero_exit_is_not_a_permission_or_input_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with service.server.session_manager.run(), http_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "print('secret-output-permission-denied'); raise SystemExit(7)",
                        ]
                    },
                )
            )
            assert result["exit_code"] == 7
            assert result["error_code"] == "COMMAND_EXIT_NONZERO"
            assert result["execution_started"] is True

    asyncio.run(scenario())
    logs = "\n".join(
        MCPLogFormatter().format(r) for r in caplog.records if hasattr(r, "mcp_event")
    )
    assert "secret-output-permission-denied" not in logs
    assert "COMMAND_EXIT_NONZERO" in logs


def test_real_os_execution_denial_has_a_distinct_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\necho should-not-run\n")
    script.chmod(0o600)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with service.server.session_manager.run(), http_client(service) as client:
            result = structured(
                await client.call_tool("exec_command", {"argv": [str(script)]})
            )
            assert result["error_code"] == "OS_PERMISSION_DENIED"
            assert result["execution_started"] is None
            assert "should-not-run" not in result.get("output", "")

    asyncio.run(scenario())
    logs = [
        json.loads(MCPLogFormatter().format(r))
        for r in caplog.records
        if hasattr(r, "mcp_event")
    ]
    assert logs[-1]["error_code"] == "OS_PERMISSION_DENIED"


def test_running_command_and_timeout_keep_correct_outcomes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with service.server.session_manager.run(), http_client(service) as client:
            started = structured(
                await client.call_tool(
                    "exec_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import time\nfrom pathlib import Path\n"
                            "deadline = time.monotonic() + 10\n"
                            "while not Path('release-command').exists():\n"
                            "    assert time.monotonic() < deadline, 'release not received'\n"
                            "    time.sleep(0.01)\n",
                        ],
                        "yield_time_ms": 1,
                    },
                )
            )
            assert started["running"] is True
            assert started["next_tool"] == "process_read"
            (tmp_path / "release-command").write_text("release\n")
            cursor = {"session_id": started["session_id"]}
            while True:
                result = structured(
                    await client.call_tool(
                        "process_read", {"sessions": [cursor], "wait_ms": 1000}
                    )
                )
                item = result["items"][0]
                if not item["continue"]:
                    assert item["exit_code"] == 0
                    break
                cursor = item["next_cursor"]
            timed_out = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": "import time; time.sleep(5)",
                        "timeout": 1,
                    },
                )
            )
            assert timed_out["error_code"] == "COMMAND_TIMEOUT"
            assert timed_out["execution_started"] is True

    asyncio.run(scenario())
    logs = [
        json.loads(MCPLogFormatter().format(r))
        for r in caplog.records
        if hasattr(r, "mcp_event")
    ]
    assert any(event["outcome"] == "running" for event in logs)
    assert logs[-1]["error_code"] == "COMMAND_TIMEOUT"
