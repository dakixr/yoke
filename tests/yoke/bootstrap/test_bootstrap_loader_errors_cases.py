"""Bootstrap loader error reporting test cases for yoke."""

# ruff: noqa: D103, S101

from __future__ import annotations

from pathlib import Path

import pytest

from yoke.cli.bootstrap.config import ToolDiscoveryProvider
from yoke.cli.bootstrap.config import resolve_agent_config


def test_invalid_global_config_reports_human_readable_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".yoke").mkdir(parents=True)
    (home / ".yoke" / "config.json").write_text(
        """{
  "tools": {
    "read": "allow",
    / "grep": "allow"
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    with pytest.raises(ValueError) as exc_info:
        resolve_agent_config(
            root=tmp_path,
            base_system_prompt=None,
            include_global_tools=False,
            home=home,
            provider=ToolDiscoveryProvider(),
        )

    message = str(exc_info.value)
    assert f"Invalid yoke config file `{home / '.yoke' / 'config.json'}`." in message
    assert "Invalid JSON syntax." in message
    assert "Expected shape:" in message


def test_invalid_repo_tool_plugin_does_not_block_startup(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "broken.py").write_text(
        "raise RuntimeError('boom during import')\n",
        encoding="utf-8",
    )

    config = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
        home=tmp_path / "home",
        provider=ToolDiscoveryProvider(),
    )

    assert config.tools
    assert all(tool.name != "broken" for tool in config.tools)


def test_repo_tool_plugin_can_relative_import_sibling_module(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "helper.py").write_text(
        """
from __future__ import annotations


def tool_text() -> str:
    return "from helper"
""".lstrip(),
        encoding="utf-8",
    )
    (tools_dir / "relative_tool.py").write_text(
        """
from __future__ import annotations

from yoke.cli.tools.decorators import function_tool

from .helper import tool_text


@function_tool
def relative_echo() -> dict[str, object]:
    return {"ok": True, "text": tool_text()}
""".lstrip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
        home=tmp_path / "home",
        provider=ToolDiscoveryProvider(),
    )

    tool_names = {entry.tool.name for entry in resolved.tool_report.discovered_tools}
    assert "relative_echo" in tool_names


def test_global_tool_plugin_can_import_cli_tool_decorators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    tools_dir = home / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "legacy_tool.py").write_text(
        """
from __future__ import annotations

from yoke.cli.tools.decorators import function_tool


@function_tool
def legacy_echo() -> dict[str, object]:
    return {"ok": True}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=True,
        home=home,
        provider=ToolDiscoveryProvider(),
    )

    tool_names = {entry.tool.name for entry in resolved.tool_report.discovered_tools}
    assert "legacy_echo" in tool_names
