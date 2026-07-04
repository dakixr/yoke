# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from yoke.agent.tools import McpCallTool
from yoke.agent.tools import McpInspectTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.capabilities.builtin import McpCapability
from yoke.cli.bootstrap.tools import create_builtin_tools
from yoke.cli.interactive.mcp_menu import McpMenuScope
from yoke.cli.interactive.mcp_menu import _set_mcp_server_enabled
from yoke.cli.interactive.mcp_menu import _toggle_mcp_tool
from yoke.mcp import McpManager
from yoke.mcp import McpSessionPolicy
from yoke.mcp import load_mcp_config
from yoke.ai.providers.base import Provider


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def write_fake_mcp_server(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            r"""
            import json
            import sys

            def send(payload):
                sys.stdout.write(json.dumps(payload) + "\n")
                sys.stdout.flush()

            for line in sys.stdin:
                message = json.loads(line)
                method = message.get("method")
                message_id = message.get("id")
                if method == "initialize":
                    send({
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake", "version": "1"},
                        },
                    })
                elif method == "tools/list":
                    send({
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "tools": [
                                {
                                    "name": "echo",
                                    "description": "Echo a message.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"message": {"type": "string"}},
                                        "required": ["message"],
                                        "$defs": {"shared": {"type": "string"}},
                                    },
                                },
                                {
                                    "name": "hidden",
                                    "description": "Hidden tool.",
                                    "inputSchema": {"type": "object"},
                                },
                            ]
                        },
                    })
                elif method == "tools/call":
                    params = message.get("params", {})
                    arguments = params.get("arguments", {})
                    send({
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "content": [{"type": "text", "text": arguments.get("message", "")}],
                            "structuredContent": {"tool": params.get("name")},
                        },
                    })
                elif message_id is not None:
                    send({"jsonrpc": "2.0", "id": message_id, "result": {}})
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def write_mcp_config(root: Path, server_path: Path) -> None:
    config_dir = root / ".yoke"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "fake": {
                        "description": "Fake MCP server.",
                        "command": sys.executable,
                        "args": [str(server_path)],
                        "enabled_tools": ["echo"],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_mcp_config_loads_repo_server(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)

    config = load_mcp_config(root=tmp_path, home=tmp_path / "home")

    assert [server.name for server in config.servers] == ["fake"]
    assert config.servers[0].description == "Fake MCP server."
    assert config.servers[0].command == sys.executable
    assert config.servers[0].enabled_tools == ("echo",)


def test_builtin_tools_include_compact_mcp_facade_when_configured(
    tmp_path: Path,
) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    provider = SimpleNamespace()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path / "home",
        provider=cast(Any, provider),
        model=ModelIdentity(provider_name="demo", model_id="gpt-demo"),
    )

    tools = create_builtin_tools(context)
    names = [tool.name for tool in tools]

    assert "mcp_inspect" in names
    assert "mcp_call" in names
    assert sum(name.startswith("mcp_") for name in names) == 2
    inspect = next(tool for tool in tools if tool.name == "mcp_inspect")
    definition = as_dict(inspect.to_definition()["function"])
    parameters = as_dict(definition["parameters"])
    properties = as_dict(parameters["properties"])
    server_property = as_dict(properties["server"])
    assert "fake (Fake MCP server.)" in definition["description"]
    assert server_property["enum"] == ["fake"]
    assert parameters["required"] == ["server"]


def test_mcp_tools_inspect_and_call_stdio_server(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    manager = McpManager.from_paths(root=tmp_path, home=tmp_path / "home")
    inspect_tool = McpInspectTool.bind(mcp_manager=manager)
    call_tool = McpCallTool.bind(mcp_manager=manager)

    try:
        inspected = as_dict(inspect_tool.parse_arguments({"server": "fake"}).execute())
        called = as_dict(
            call_tool.parse_arguments(
                {
                    "server": "fake",
                    "tool": "echo",
                    "arguments": {"message": "hello"},
                }
            ).execute()
        )
        hidden = as_dict(
            call_tool.parse_arguments(
                {"server": "fake", "tool": "hidden", "arguments": {}}
            ).execute()
        )
    finally:
        manager.close()

    servers = cast(list[dict[str, object]], inspected["servers"])
    tools = cast(list[dict[str, object]], servers[0]["tools"])
    assert [tool["name"] for tool in tools] == ["echo"]
    assert tools[0]["input_schema"] == {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "$defs": {"shared": {"type": "string"}},
    }
    assert called["ok"] is True
    assert called["content"] == 'hello\n\nStructured content:\n{\n  "tool": "echo"\n}'
    assert hidden["ok"] is False
    assert hidden["error"] == "MCP tool is disabled: fake/hidden"


def test_mcp_inspect_rejects_unknown_server(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    manager = McpManager.from_paths(root=tmp_path, home=tmp_path / "home")
    inspect_tool = McpInspectTool.bind(mcp_manager=manager)

    try:
        try:
            inspect_tool.parse_arguments({"server": "missing"})
        except ValueError as exc:
            assert "Expected one of: fake" in str(exc)
        else:
            raise AssertionError("expected invalid MCP server to be rejected")
    finally:
        manager.close()


def test_runtime_refreshes_mcp_tools_from_current_config(tmp_path: Path) -> None:
    class CaptureToolsProvider(Provider):
        def __init__(self) -> None:
            self.tools: list[dict[str, object]] = []

        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            del messages
            self.tools = tools
            return Message.assistant("done")

    provider = CaptureToolsProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        capabilities=(McpCapability(),),
        tool_root=tmp_path,
        tool_home=tmp_path / "home",
    )
    assert "mcp_inspect" not in agent.tools

    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    agent.run("hello")

    inspect_definition = next(
        tool
        for tool in provider.tools
        if as_dict(tool["function"])["name"] == "mcp_inspect"
    )
    function = as_dict(inspect_definition["function"])
    parameters = as_dict(function["parameters"])
    properties = as_dict(parameters["properties"])
    assert as_dict(properties["server"])["enum"] == ["fake"]


def test_mcp_inspect_query_matching_server_keeps_tools(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "chrome-devtools": {
                        "command": sys.executable,
                        "args": [str(server_path)],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    manager = McpManager.from_paths(root=tmp_path, home=tmp_path / "home")

    try:
        inspected = manager.inspect(query="chrome", include_schemas=True)
    finally:
        manager.close()

    servers = cast(list[dict[str, object]], inspected["servers"])
    tools = cast(list[dict[str, object]], servers[0]["tools"])
    assert servers[0]["name"] == "chrome-devtools"
    assert [tool["name"] for tool in tools] == ["echo", "hidden"]
    assert tools[0]["input_schema"] == {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "$defs": {"shared": {"type": "string"}},
    }


def test_mcp_session_policy_can_disable_server(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    config = load_mcp_config(root=tmp_path, home=tmp_path / "home")
    session_policy = McpSessionPolicy.empty()

    _set_mcp_server_enabled(
        root=tmp_path,
        scope=McpMenuScope(
            id="session",
            label="This session",
            description="Temporary",
        ),
        server=config.servers[0],
        enabled=False,
        session_policy=session_policy,
    )
    effective = load_mcp_config(
        root=tmp_path,
        home=tmp_path / "home",
        session_policy=session_policy,
    )

    assert effective.servers[0].enabled is False
    assert json.loads((tmp_path / ".yoke" / "mcp.json").read_text())["mcp_servers"][
        "fake"
    ]["enabled_tools"] == ["echo"]


def test_mcp_repo_policy_can_override_global_server(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    home = tmp_path / "home"
    global_dir = home / ".yoke"
    global_dir.mkdir(parents=True)
    (global_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(server_path)],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_mcp_config(root=tmp_path, home=home)
    repo_path = tmp_path / ".yoke" / "mcp.json"

    _set_mcp_server_enabled(
        root=tmp_path,
        scope=McpMenuScope(
            id="repo",
            label="This repo",
            description="Repo",
            path=repo_path,
        ),
        server=config.servers[0],
        enabled=False,
        session_policy=McpSessionPolicy.empty(),
    )
    payload = json.loads(repo_path.read_text(encoding="utf-8"))
    effective = load_mcp_config(root=tmp_path, home=home)

    assert payload["mcp_servers"]["fake"]["command"] == sys.executable
    assert payload["mcp_servers"]["fake"]["enabled"] is False
    assert effective.servers[0].enabled is False


def test_mcp_tool_toggle_preserves_enabled_tools_semantics(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    config = load_mcp_config(root=tmp_path, home=tmp_path / "home")
    repo_path = tmp_path / ".yoke" / "mcp.json"

    _toggle_mcp_tool(
        root=tmp_path,
        scope=McpMenuScope(
            id="repo",
            label="This repo",
            description="Repo",
            path=repo_path,
        ),
        server=config.servers[0],
        tool_name="hidden",
        session_policy=McpSessionPolicy.empty(),
    )
    payload = json.loads(repo_path.read_text(encoding="utf-8"))
    effective = load_mcp_config(root=tmp_path, home=tmp_path / "home")

    assert payload["mcp_servers"]["fake"]["enabled_tools"] == ["echo", "hidden"]
    assert effective.servers[0].enabled_tools == ("echo", "hidden")


def _start_mock_streamable_http_server() -> tuple[Any, int]:
    """Start a minimal MCP Streamable HTTP server in a background thread."""
    import http.server
    import socketserver

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002, ARG002
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            message = json.loads(body)
            method = message.get("method")
            message_id = message.get("id")
            session_id = self.headers.get("Mcp-Session-Id")
            if method == "initialize":
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-http", "version": "1"},
                    },
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "test-session-123")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            if not session_id:
                self.send_error(400, "Missing session")
                return
            if method == "tools/list":
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo back.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                    "required": ["message"],
                                },
                            }
                        ]
                    },
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            if method == "tools/call":
                params = message.get("params", {})
                args = params.get("arguments", {})
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": [{"type": "text", "text": args.get("message", "")}]
                    },
                }
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"data: {json.dumps(response)}\n\n".encode())
                self.wfile.flush()
                return
            if message_id is not None:
                response = {"jsonrpc": "2.0", "id": message_id, "result": {}}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            self.send_response(202)
            self.end_headers()

    server = socketserver.TCPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_streamable_http_client_initialize_list_and_call() -> None:
    from yoke.mcp.client import StreamableHttpClient
    from yoke.mcp.config import McpServerConfig

    server, port = _start_mock_streamable_http_server()
    try:
        config = McpServerConfig(
            name="mock",
            transport="streamable-http",
            url=f"http://127.0.0.1:{port}/mcp",
            startup_timeout_sec=5.0,
            tool_timeout_sec=5.0,
        )
        client = StreamableHttpClient(config, root=Path("/tmp"))
        try:
            client.start()
            assert client._session_id == "test-session-123"
            tools = client.list_tools()
            assert [tool.name for tool in tools] == ["echo"]
            assert tools[0].input_schema["required"] == ["message"]
            result = client.call_tool("echo", {"message": "hello"})
            content = result["content"]
            assert content[0]["text"] == "hello"
        finally:
            client.close()
    finally:
        server.shutdown()


def test_create_mcp_client_selects_by_transport() -> None:
    from yoke.mcp.client import StreamableHttpClient
    from yoke.mcp.client import StdioMcpClient
    from yoke.mcp.client import create_mcp_client
    from yoke.mcp.config import McpServerConfig

    stdio_config = McpServerConfig(
        name="s",
        transport="stdio",
        command="python3",
    )
    http_config = McpServerConfig(
        name="h",
        transport="streamable-http",
        url="http://localhost:1/mcp",
    )
    assert isinstance(
        create_mcp_client(stdio_config, root=Path("/tmp")), StdioMcpClient
    )
    assert isinstance(
        create_mcp_client(http_config, root=Path("/tmp")), StreamableHttpClient
    )
