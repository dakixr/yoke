"""Terminal menus read and mutate the in-test daemon's catalogs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yoke.cli.interactive import mcp_menu, tools_menu
from yoke.cli.render import build_console
from yoke.mcp.manager import McpManager, McpToolInfo

from .support import CaptureStream
from .terminal_daemon_support import TerminalDaemon, terminal as terminal


@pytest.mark.parametrize("scope", ["session", "root", "global"])
def test_tools_menu_toggles_daemon_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: TerminalDaemon,
    scope: str,
) -> None:
    backend = terminal.backend("tools", root=tmp_path)
    before = backend.tools().active_names()
    chosen = set()

    def select(rows, **kwargs):
        nonlocal chosen
        selected = set(kwargs["selected_indexes"])
        index = next(i for i, row in enumerate(rows) if "read" in row.tool_names)
        chosen = rows[index].tool_names
        assert chosen <= before
        assert tools_menu._render_tool_row(
            rows[index], index, True, True, kwargs["columns"]
        ).startswith("[x]")
        return selected - {index}

    monkeypatch.setattr(tools_menu, "select_table_items_interactive", select)
    monkeypatch.setattr(
        tools_menu,
        "_select_tool_change_scope",
        lambda **kwargs: tools_menu.ToolChangeScope(scope, scope, "test"),
    )
    stream = CaptureStream()
    tools_menu.handle_tools_menu(
        agent=backend, console=build_console(stream), root=tmp_path
    )
    assert backend.tools().active_names() == before - chosen
    assert "disabled" in stream.getvalue()
    path = (Path.home() if scope == "global" else tmp_path) / ".yoke" / "config.json"
    if scope == "session":
        assert not path.exists()
    else:
        config = json.loads(path.read_text())
        assert config["capabilities"]["file.read"] == "deny"


@pytest.mark.parametrize(
    "scope,action",
    [("session", "server"), ("repo", "server"), ("global", "tool")],
)
def test_mcp_menu_changes_daemon_policy_and_persisted_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: TerminalDaemon,
    scope: str,
    action: str,
) -> None:
    config_path = (
        (Path.home() if scope == "global" else tmp_path) / ".yoke" / "mcp.json"
    )
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"mcpServers": {"fixture": {"command": "unused"}}})
    )
    monkeypatch.setattr(
        McpManager,
        "inspect",
        lambda *args, **kwargs: {
            "servers": [
                {
                    "name": "fixture",
                    "status": "ready",
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo the input",
                            "input_schema": {},
                        }
                    ],
                }
            ]
        },
    )
    monkeypatch.setattr(
        McpManager,
        "list_configured_tools",
        lambda *args, **kwargs: (
            McpToolInfo(name="echo", description="Echo the input", input_schema={}),
        ),
    )
    backend = terminal.backend("mcp", root=tmp_path)
    monkeypatch.setattr(mcp_menu, "_select_mcp_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp_menu,
        "_select_mcp_server_action",
        lambda server: mcp_menu.McpServerAction(
            "tools" if action == "tool" else scope, scope, "test"
        ),
    )
    selections = iter([0, None])
    monkeypatch.setattr(
        mcp_menu,
        "select_table_item_interactive",
        lambda rows, **kwargs: rows[0] if next(selections) == 0 else None,
    )
    monkeypatch.setattr(
        mcp_menu,
        "_select_mcp_scope",
        lambda **kwargs: mcp_menu._scope_for_action(scope, root=tmp_path),
    )
    stream = CaptureStream()
    mcp_menu.handle_mcp_menu(
        agent=backend,
        console=build_console(stream),
        root=tmp_path,
        initial_server="fixture",
    )
    server = backend.mcp(tmp_path).config().servers[0]
    if action == "server":
        assert not server.enabled
        assert "disabled" in stream.getvalue()
    else:
        assert server.disabled_tools == ("echo",)
        assert "toggled" in stream.getvalue()
    if scope == "session":
        assert json.loads(config_path.read_text()) == {
            "mcpServers": {"fixture": {"command": "unused"}}
        }
    else:
        written = (
            (Path.home() / ".yoke" / "mcp.json") if scope == "global" else config_path
        )
        assert written.exists()
        assert (
            "false" in written.read_text()
            if action == "server"
            else "echo" in written.read_text()
        )


@pytest.mark.parametrize(
    "allowlist",
    [None, []],
)
def test_mcp_menu_can_reenable_a_disabled_tool(
    tmp_path: Path, monkeypatch, terminal, allowlist
) -> None:
    config = tmp_path / ".yoke" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": {
                        "command": "unused",
                        "enabled_tools": allowlist,
                        "disabled_tools": ["echo"],
                    }
                }
            }
        )
    )
    monkeypatch.setattr(
        McpManager,
        "list_configured_tools",
        lambda *args, **kwargs: (
            McpToolInfo(name="echo", description="Echo the input", input_schema={}),
        ),
    )
    backend = terminal.backend("reenable", root=tmp_path)
    selections = iter([0, None])
    monkeypatch.setattr(mcp_menu, "_select_mcp_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp_menu,
        "_select_mcp_server_action",
        lambda server: mcp_menu.McpServerAction("tools", "tools", "test"),
    )

    def select(rows, **kwargs):
        assert rows[0].name == "echo"
        value = next(selections)
        assert rows[0].enabled is (value is None)
        return rows[0] if value == 0 else None

    monkeypatch.setattr(mcp_menu, "select_table_item_interactive", select)
    monkeypatch.setattr(
        mcp_menu,
        "_select_mcp_scope",
        lambda **kwargs: mcp_menu._scope_for_action("session", root=tmp_path),
    )
    stream = CaptureStream()
    mcp_menu.handle_mcp_menu(
        agent=backend,
        console=build_console(stream),
        root=tmp_path,
        initial_server="fixture",
    )
    server = backend.mcp(tmp_path).config().servers[0]
    assert server.disabled_tools == ()
    assert server.enabled_tools == (("echo",) if allowlist is not None else None)
    assert "toggled" in stream.getvalue()
