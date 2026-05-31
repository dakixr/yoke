"""Tool discovery helpers for yoke CLI bootstrap."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import cast

from yoke.agent.tools import ApplyPatchTool
from yoke.agent.tools import AttachImageTool
from yoke.agent.tools import CommandTool
from yoke.agent.tools import EditTool
from yoke.agent.tools import ExtractFileContextTool
from yoke.agent.tools import FindTool
from yoke.agent.tools import GrepTool
from yoke.agent.tools import LocalTool
from yoke.agent.tools import LsTool
from yoke.agent.tools import PythonExecTool
from yoke.agent.tools import ReadTool
from yoke.agent.tools import RipgrepTool
from yoke.agent.tools import SubagentTool
from yoke.agent.tools import WebFetchTool
from yoke.agent.tools import WebResearchTool
from yoke.cli.bootstrap.types import LoadedTool
from yoke.cli.bootstrap.types import RegisterToolsFunc
from yoke.cli.bootstrap.types import ToolPluginContext
from yoke.cli.bootstrap.types import ToolSourceKind


def _tool_scope_label(source_kind: ToolSourceKind) -> str:
    return "global ~/.yoke" if source_kind == "global" else "repo .yoke"


def load_tools(
    *,
    root: Path,
    home: Path,
    include_repo_tools: bool,
    include_global_tools: bool,
    cancel_requested=None,
) -> list[LoadedTool]:
    """Load built-in and plugin tools."""
    builtin_tools = create_builtin_tools(root, cancel_requested=cancel_requested)
    plugin_context = ToolPluginContext(
        root=root,
        home=home,
        cancel_requested=cancel_requested,
    )
    loaded_tools: list[LoadedTool] = [
        LoadedTool(tool=tool, source_kind="default", source_label="default:builtin")
        for tool in builtin_tools
    ]
    if include_global_tools:
        loaded_tools.extend(
            _load_tools_from_directory(
                home / ".yoke",
                plugin_context,
                source_kind="global",
            )
        )
    if include_repo_tools:
        loaded_tools.extend(
            _load_tools_from_directory(
                root / ".yoke",
                plugin_context,
                source_kind="repo",
            )
        )
    return loaded_tools


def create_builtin_tools(
    root: Path,
    *,
    cancel_requested=None,
) -> list[LocalTool]:
    """Create the default built-in tool set."""
    return [
        ReadTool.bind(root=root, cancel_requested=cancel_requested),
        ApplyPatchTool.bind(root=root, cancel_requested=cancel_requested),
        CommandTool.bind(root=root, cancel_requested=cancel_requested),
        EditTool.bind(root=root, cancel_requested=cancel_requested),
        GrepTool.bind(root=root, cancel_requested=cancel_requested),
        FindTool.bind(root=root, cancel_requested=cancel_requested),
        LsTool.bind(root=root, cancel_requested=cancel_requested),
        ExtractFileContextTool.bind(root=root, cancel_requested=cancel_requested),
        AttachImageTool.bind(root=root, cancel_requested=cancel_requested),
        WebFetchTool.bind(cancel_requested=cancel_requested),
        WebResearchTool.bind(cancel_requested=cancel_requested),
        PythonExecTool.bind(root=root, cancel_requested=cancel_requested),
        RipgrepTool.bind(root=root, cancel_requested=cancel_requested),
        SubagentTool.bind(root=root, cancel_requested=cancel_requested),
    ]


def _load_tools_from_directory(
    directory: Path,
    context: ToolPluginContext,
    *,
    source_kind: ToolSourceKind,
) -> list[LoadedTool]:
    if not directory.is_dir():
        return []
    loaded: list[LoadedTool] = []
    for path in _iter_tool_module_paths(directory):
        try:
            module = _load_tool_module(path, source_kind=source_kind)
            register_tools = getattr(module, "register_tools", None)
            if callable(register_tools):
                tools = _call_register_tools(
                    cast(RegisterToolsFunc, register_tools),
                    context=context,
                    path=path,
                    source_kind=source_kind,
                )
            else:
                tools = _discover_module_tools(
                    module,
                    context=context,
                    path=path,
                    source_kind=source_kind,
                )
        except Exception:  # noqa: S112
            continue
        for tool in tools:
            loaded.append(
                LoadedTool(
                    tool=tool,
                    source_kind=source_kind,
                    source_label=f"{source_kind}:{path}",
                    source_path=path,
                )
            )
    return loaded


def _iter_tool_module_paths(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        yield path


def _load_tool_module(path: Path, *, source_kind: ToolSourceKind) -> ModuleType:
    package_name = _ensure_tool_package(path.parent)
    module_name = f"{package_name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load tool module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(
            f"Could not load {_tool_scope_label(source_kind)} "
            f"tool plugin `{path}`. "
            f"The Python module failed to import: {exc}"
        ) from exc
    return module


def _ensure_tool_package(directory: Path) -> str:
    package_name = (
        "yoke_external_tools_"
        + hashlib.sha256(str(directory).encode("utf-8")).hexdigest()[:16]
    )
    package = sys.modules.get(package_name)
    if package is None:
        package = ModuleType(package_name)
        package.__file__ = str(directory / "__init__.py")
        package.__package__ = package_name
        package.__path__ = [str(directory)]  # type: ignore[attr-defined]
        sys.modules[package_name] = package
    return package_name


def _call_register_tools(
    register_tools: RegisterToolsFunc,
    *,
    context: ToolPluginContext,
    path: Path,
    source_kind: ToolSourceKind,
) -> list[LocalTool]:
    try:
        tools = register_tools(context)
    except Exception as exc:
        raise ValueError(
            f"Could not register tools from "
            f"{_tool_scope_label(source_kind)} plugin `{path}`. "
            f"`register_tools(context)` raised: {exc}"
        ) from exc
    try:
        tool_list = list(tools)
    except TypeError as exc:
        raise ValueError(
            f"Tool plugin `{path}` is invalid. "
            "`register_tools(context)` must return an iterable "
            "of yoke tools."
        ) from exc
    invalid = [tool for tool in tool_list if not isinstance(tool, LocalTool)]
    if invalid:
        raise ValueError(
            f"Tool plugin `{path}` is invalid. "
            "`register_tools(context)` returned objects "
            "that are not yoke tools."
        )
    return tool_list


def _discover_module_tools(
    module: ModuleType,
    *,
    context: ToolPluginContext,
    path: Path,
    source_kind: ToolSourceKind,
) -> list[LocalTool]:
    tools: list[LocalTool] = []
    for value in module.__dict__.values():
        if not isinstance(value, type):
            continue
        if not issubclass(value, LocalTool) or value is LocalTool:
            continue
        if value.__module__ != module.__name__ or not value.is_yoke_tool:
            continue
        try:
            tools.append(
                value.bind(
                    root=context.root,
                    home=context.home,
                    cancel_requested=context.cancel_requested,
                )
            )
        except Exception as exc:
            raise ValueError(
                f"Could not initialize tool `{value.__name__}` "
                f"from {_tool_scope_label(source_kind)} "
                f"plugin `{path}`: {exc}"
            ) from exc
    return tools


def resolve_tool_overrides(loaded_tools: list[LoadedTool]) -> list[LoadedTool]:
    """Resolve plugin overrides by source precedence."""
    seen: dict[str, LoadedTool] = {}
    for entry in loaded_tools:
        existing = seen.get(entry.tool.name)
        if existing is not None:
            current_priority = _tool_source_priority(entry.source_kind)
            existing_priority = _tool_source_priority(existing.source_kind)
            if current_priority == existing_priority:
                raise ValueError(
                    f"Conflicting tool name {entry.tool.name!r} from "
                    f"{entry.source_label}; already registered by "
                    f"{existing.source_label}. Same-precedence tools cannot "
                    "override each other."
                )
            if current_priority < existing_priority:
                continue
        seen[entry.tool.name] = entry
    return list(seen.values())


def _tool_source_priority(source_kind: ToolSourceKind) -> int:
    return {"default": 0, "global": 1, "repo": 2}[source_kind]
