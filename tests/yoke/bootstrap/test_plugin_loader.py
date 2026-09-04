from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

import os
from pathlib import Path

from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.cli.bootstrap import tools as tools_module
from yoke.cli.bootstrap.config import ToolDiscoveryProvider
from yoke.cli.bootstrap.plugin_loader import _iter_tool_module_paths
from yoke.cli.bootstrap.types import LoadedToolGroup


def test_tool_discovery_only_walks_explicit_plugin_locations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / ".yoke"
    expected = [
        directory / "another_tool.py",
        directory / "root_tool.py",
        directory / "tools" / "nested" / "nested_tool.py",
    ]
    for path in expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
    ignored = [
        "cache",
        ".agents_local",
        "providers",
        "sessions",
        "skills",
        "usage-metric-logs",
        "anything-else",
    ]
    blocked_paths = []
    for name in ignored:
        blocked = directory / name
        blocked_paths.append(blocked)
        nested = blocked / "nested"
        nested.mkdir(parents=True)
        (nested / "must_not_load.py").write_text("VALUE = 2\n", encoding="utf-8")

    original_scandir = os.scandir

    def guarded_scandir(path):
        candidate = Path(path)
        assert not any(
            candidate == blocked or blocked in candidate.parents
            for blocked in blocked_paths
        )
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    assert list(_iter_tool_module_paths(directory)) == sorted(expected)


def test_tool_discovery_ignores_private_and_package_modules(tmp_path: Path) -> None:
    directory = tmp_path / ".yoke"
    tools = directory / "tools"
    tools.mkdir(parents=True)
    expected = [directory / "direct.py", tools / "visible.py"]
    for path in [
        *expected,
        directory / "_private.py",
        directory / "__init__.py",
        tools / "_private.py",
        tools / "__init__.py",
    ]:
        path.write_text("VALUE = 1\n", encoding="utf-8")

    assert list(_iter_tool_module_paths(directory)) == expected


def test_global_tool_directory_is_not_loaded_twice_when_root_is_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[Path, str]] = []

    def load_group(directory, _context, *, source_kind):
        calls.append((directory, source_kind))
        return LoadedToolGroup(tools=[], system_messages=[])

    monkeypatch.setattr(
        tools_module, "create_builtin_capabilities", lambda _context: []
    )
    monkeypatch.setattr(tools_module, "_load_plugin_group", load_group)
    provider = ToolDiscoveryProvider()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=provider,
        model=ModelIdentity(provider_name=provider.provider_name),
    )

    tools_module.load_tools(
        root=tmp_path,
        home=tmp_path,
        include_repo_tools=True,
        include_global_tools=True,
        context=context,
    )

    assert calls == [(tmp_path / ".yoke", "global")]
