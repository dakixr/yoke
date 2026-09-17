"""Every adapter exit is logged without exposing arbitrary request data."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from yoke.mcp_server.commands import MCPExecCommandTool
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.diagnostics import MCPLogFormatter, configure_logging
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(MCPLogFormatter().format(r))
        for r in caplog.records
        if hasattr(r, "mcp_event")
    ]


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("python_exec", {"code": []}),
        ("process_read", {}),
        ("read_file", {}),
        ("mcp_call", {}),
    ],
)
def test_all_outer_validation_failures_are_logged(
    tmp_path: Path,
    name: str,
    arguments: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            payload = structured(await client.call_tool(name, arguments))
            assert payload["error_code"] == "INVALID_ARGUMENT"
            assert payload["execution_started"] is False

    asyncio.run(scenario())
    assert len(events(caplog)) == 2
    assert events(caplog)[-1]["tool"] == name
    assert events(caplog)[-1]["error_code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeError("sensitive-exception-value"), "TOOL_EXECUTION_ERROR"),
        (PermissionError("sensitive-exception-value"), "OS_PERMISSION_DENIED"),
    ],
)
def test_runtime_exceptions_do_not_claim_execution_was_prevented(
    tmp_path: Path,
    error: Exception,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))

        async def crash(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise error

        monkeypatch.setattr(service.runtime, "execute", crash)
        async with memory_client(service) as client:
            payload = structured(await client.call_tool("command_exec", {"cmd": "pwd"}))
            assert payload["error_code"] == code
            assert payload["execution_started"] is None
            assert payload["stage"] == "execution"

    asyncio.run(scenario())
    logged = events(caplog)
    assert len(logged) == 2
    assert logged[-1]["exception_type"] == type(error).__name__
    assert "sensitive-exception-value" not in json.dumps(logged)


def test_post_dispatch_validation_is_not_reported_as_safe_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))

        async def crash(*_args: object, **_kwargs: object) -> dict[str, object]:
            MCPExecCommandTool.model_validate({})
            raise AssertionError("Expected a validation error")

        monkeypatch.setattr(service.runtime, "execute", crash)
        async with memory_client(service) as client:
            payload = structured(await client.call_tool("command_exec", {"cmd": "pwd"}))
            assert payload["error_code"] == "TOOL_EXECUTION_ERROR"
            assert payload["execution_started"] is None

    asyncio.run(scenario())
    assert events(caplog)[-1]["exception_type"] == ValidationError.__name__


def test_unknown_names_and_arguments_cannot_leak_into_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            payload = structured(
                await service.adapter.call_tool(
                    "secret-tool-name", {"secret-field-name": "secret-value"}
                )
            )
            assert payload["error_code"] == "UNKNOWN_TOOL"
            assert payload["execution_started"] is False

    asyncio.run(scenario())
    assert len(events(caplog)) == 2
    assert "secret-" not in json.dumps(events(caplog))


def test_cancellation_has_a_terminal_request_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="yoke.mcp_server.diagnostics")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))

        async def cancel(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise asyncio.CancelledError

        monkeypatch.setattr(service.runtime, "execute", cancel)
        async with memory_client(service):
            with pytest.raises(asyncio.CancelledError):
                await service.adapter.call_tool("command_exec", {"cmd": "pwd"})

    asyncio.run(scenario())
    assert len(events(caplog)) == 2
    assert events(caplog)[-1]["outcome"] == "interrupted"


def test_cli_formatter_preserves_metadata_and_redacts_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: dict[str, Any] = {}
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: configured.update(kwargs)
    )
    configure_logging("info")
    handler = configured["handlers"][0]
    assert isinstance(handler.formatter, MCPLogFormatter)
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "request failed",
        (),
        (ValueError, ValueError("secret-exception"), None),
    )
    record.mcp_event = {"tool": "command_exec", "ok": False, "duration_ms": 7}
    result = json.loads(handler.format(record))
    assert result["tool"] == "command_exec"
    assert result["ok"] is False
    assert result["duration_ms"] == 7
    assert result["exception_type"] == "ValueError"
    assert "secret-exception" not in json.dumps(result)
