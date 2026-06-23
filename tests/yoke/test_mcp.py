# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from yoke.agent.tools import McpCallTool
from yoke.agent.tools import McpInspectTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.cli.bootstrap.tools import create_builtin_tools
from yoke.cli.interactive.mcp_menu import McpMenuScope
from yoke.cli.interactive.mcp_menu import _set_mcp_server_enabled
from yoke.cli.interactive.mcp_menu import _toggle_mcp_tool
from yoke.mcp import McpManager
from yoke.mcp import McpSessionPolicy
from yoke.mcp import load_mcp_config


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


def test_mcp_tools_inspect_and_call_stdio_server(tmp_path: Path) -> None:
    server_path = tmp_path / "server.py"
    write_fake_mcp_server(server_path)
    write_mcp_config(tmp_path, server_path)
    manager = McpManager.from_paths(root=tmp_path, home=tmp_path / "home")
    inspect_tool = McpInspectTool.bind(mcp_manager=manager)
    call_tool = McpCallTool.bind(mcp_manager=manager)

    try:
        inspected = as_dict(inspect_tool.parse_arguments({}).execute())
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
    assert called["ok"] is True
    assert called["content"] == 'hello\n\nStructured content:\n{\n  "tool": "echo"\n}'
    assert hidden["ok"] is False
    assert hidden["error"] == "MCP tool is disabled: fake/hidden"


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
