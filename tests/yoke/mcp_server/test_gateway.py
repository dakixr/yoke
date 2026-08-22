"""ChatGPT-facing gateway tests for configured downstream MCP servers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

from yoke.mcp.client import StdioMcpClient
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client
from .helpers import structured


def _write_fake_server(path: Path) -> None:
    path.write_text(
        """import json
import sys

tools = [
    {
        "name": "echo",
        "description": "Echo one value.",
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
    {
        "name": "blocked",
        "description": "A tool disabled by Yoke config.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        params = request.get("params", {})
        value = params.get("arguments", {}).get("value", "")
        result = {
            "content": [{"type": "text", "text": f"echo:{value}"}],
            "structuredContent": {"value": value},
            "isError": False,
        }
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""",
        encoding="utf-8",
    )


def _write_config(root: Path, server_script: Path) -> None:
    config_dir = root / ".yoke"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "fake": {
                        "command": sys.executable,
                        "args": ["-u", str(server_script)],
                        "enabled_tools": ["echo"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_gateway_inspects_and_calls_configured_stdio_mcp(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "root"
    home.mkdir()
    root.mkdir()
    server_script = tmp_path / "fake_mcp.py"
    _write_fake_server(server_script)
    _write_config(root, server_script)
    monkeypatch.setenv("HOME", str(home))

    async def scenario() -> subprocess.Popen[str]:
        service = create_service(MCPServerConfig(root=root))
        assert service.downstream_manager is not None
        async with memory_client(service) as client:
            inspected = structured(
                await client.call_tool(
                    "mcp_inspect",
                    {"server": "fake", "include_schemas": True},
                )
            )
            assert inspected["ok"] is True
            server = inspected["servers"][0]
            assert server["status"] == "ready"
            assert [tool["name"] for tool in server["tools"]] == ["echo"]
            assert server["tools"][0]["input_schema"]["required"] == ["value"]

            called = structured(
                await client.call_tool(
                    "mcp_call",
                    {"server": "fake", "tool": "echo", "arguments": {"value": "hi"}},
                )
            )
            assert called["ok"] is True
            assert "echo:hi" in called["content"]
            assert called["structuredContent"] == {"value": "hi"}

            blocked_result = await client.call_tool(
                "mcp_call",
                {"server": "fake", "tool": "blocked", "arguments": {}},
            )
            blocked = structured(blocked_result)
            assert blocked_result.is_error is True
            assert blocked["error"] == "MCP tool is disabled: fake/blocked"

            raw_client = service.downstream_manager._clients["fake"]
            assert isinstance(raw_client, StdioMcpClient)
            process = raw_client._process
            assert process is not None
            return process

    process = asyncio.run(scenario())
    assert process.returncode is not None
