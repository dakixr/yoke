from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

import os
from pathlib import Path
import sys

import pytest

from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.cli.bootstrap import tools as tools_module
from yoke.cli.bootstrap.config import ToolDiscoveryProvider
from yoke.cli.bootstrap.plugin_loader import _iter_tool_module_paths
from yoke.cli.bootstrap.plugin_loader import load_tools_from_directory


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


@pytest.mark.parametrize(
    ("include_global_tools", "expected_scope"),
    [(True, "global"), (False, "repo")],
)
def test_shared_home_and_repo_registers_plugin_once(
    tmp_path: Path,
    monkeypatch,
    include_global_tools: bool,
    expected_scope: str,
) -> None:
    directory = tmp_path / ".yoke"
    directory.mkdir()
    plugin_path = directory / "probe.py"
    plugin_path.write_text(
        "from yoke.agent.tools import ReadTool\n"
        "def register_tools(context):\n"
        "    with (context.root / 'registrations.txt').open('a') as log:\n"
        "        log.write('registered\\n')\n"
        "    return [ReadTool.bind(root=context.root)]\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        tools_module, "create_builtin_capabilities", lambda _context: []
    )
    provider = ToolDiscoveryProvider()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=provider,
        model=ModelIdentity(provider_name=provider.provider_name),
    )

    loaded = tools_module.load_tools(
        root=tmp_path,
        home=tmp_path,
        include_repo_tools=True,
        include_global_tools=include_global_tools,
        context=context,
    )

    assert (tmp_path / "registrations.txt").read_text() == "registered\n"
    assert [entry.tool.name for entry in loaded.tools] == ["read"]
    assert loaded.tools[0].source_kind == expected_scope
    assert loaded.tools[0].source_path == plugin_path


def test_failed_import_cannot_be_reused_by_another_plugin(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / ".yoke"
    directory.mkdir()
    (directory / "broken.py").write_text(
        "VALUE = 'partly initialized'\nraise RuntimeError('not ready')\n",
        encoding="utf-8",
    )
    (directory / "dependent.py").write_text(
        "from .broken import VALUE\n"
        "from yoke.agent.tools import ReadTool\n"
        "def register_tools(context):\n"
        "    return [ReadTool.bind(root=context.root)]\n",
        encoding="utf-8",
    )
    provider = ToolDiscoveryProvider()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=provider,
        model=ModelIdentity(provider_name=provider.provider_name),
    )
    before = set(sys.modules)
    try:
        loaded = load_tools_from_directory(directory, context, source_kind="repo")

        assert loaded.tools == []
        leaked = [
            module
            for name, module in sys.modules.items()
            if name not in before
            and getattr(module, "__file__", None)
            in {str(directory / "broken.py"), str(directory / "dependent.py")}
        ]
        assert leaked == []
        assert (
            len(
                [
                    record
                    for record in caplog.records
                    if record.name == "yoke.cli.bootstrap.plugin_loader"
                ]
            )
            == 2
        )
        assert str(directory / "broken.py") in caplog.text
        assert str(directory / "dependent.py") in caplog.text
        assert "not ready" in caplog.text

        (directory / "broken.py").write_text("VALUE = 'ready'\n", encoding="utf-8")
        recovered = load_tools_from_directory(directory, context, source_kind="repo")
        assert [entry.tool.name for entry in recovered.tools] == ["read"]
        assert recovered.tools[0].source_path == directory / "dependent.py"
    finally:
        for name in set(sys.modules) - before:
            if name.startswith("yoke_external_tools_"):
                sys.modules.pop(name, None)
