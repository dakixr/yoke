"""Read-only search and late-response races at the MCP boundary."""

from __future__ import annotations

from pathlib import Path
import queue
import subprocess
from threading import Event, Thread
from typing import Any

import pytest

from yoke.mcp.client import StdioMcpClient
from yoke.mcp.config import McpServerConfig
from yoke.mcp_server.search import MCPRipgrepTool
from yoke.agent.tools.rg import RipgrepTool


def test_reader_discards_response_when_failure_fills_pending_queue(
    tmp_path: Path,
) -> None:
    client = StdioMcpClient(
        McpServerConfig(name="test", command="unused"), root=tmp_path
    )
    failure_delivered, finished = Event(), Event()

    class RacingQueue(queue.Queue[dict[str, Any]]):
        def put(self, item, block=True, timeout=None):
            if "result" in item:
                client._fail_pending("client closed")
                failure_delivered.set()
            super().put(item, block=block, timeout=timeout)

    pending = RacingQueue(maxsize=1)
    client._pending[1] = pending

    def receive() -> None:
        try:
            client._handle_message({"id": 1, "result": {"late": True}})
        finally:
            finished.set()

    reader = Thread(target=receive, daemon=True)
    reader.start()
    try:
        assert failure_delivered.wait(timeout=1)
        assert finished.wait(timeout=0.5), (
            "Reader blocked on a response queue already closed by failure"
        )
        assert pending.get_nowait() == {"error": {"message": "client closed"}}
        assert pending.empty()
    finally:
        if reader.is_alive():
            pending.get_nowait()
        reader.join(timeout=1)


@pytest.mark.parametrize(
    ("tool_class", "ignore_config"), [(MCPRipgrepTool, True), (RipgrepTool, False)]
)
def test_read_only_ripgrep_ignores_config_without_changing_agent_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_class: type[RipgrepTool],
    ignore_config: bool,
) -> None:
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(tmp_path / "untrusted-rg-config"))
    monkeypatch.setattr("yoke.agent.tools.rg._resolve_rg_binary", lambda: "rg")
    monkeypatch.setattr("yoke.agent.tools.rg.subprocess.run", run)
    tool = tool_class.bind(root=tmp_path).parse_arguments({"raw_args": "needle"})

    assert tool.execute()["ok"] is True
    assert len(calls) == 1
    assert ("--no-config" in calls[0]) is ignore_config
