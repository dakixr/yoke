"""Plugin tool loading for yoke CLI bootstrap."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import cast

from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRegistration
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRuntimeContext
from yoke.agent.tools.context import normalize_tool_registration
from yoke.cli.bootstrap.types import LoadedTool
from yoke.cli.bootstrap.types import LoadedToolGroup
from yoke.cli.bootstrap.types import LoadedSystemMessage
from yoke.cli.bootstrap.types import ToolSourceKind

type RegisterToolsFunc = Callable[[ToolRegistrationContext], ToolRegistration]
LOGGER = logging.getLogger(__name__)


def load_tools_from_directory(
    directory: Path,
    context: ToolRegistrationContext,
    *,
    source_kind: ToolSourceKind,
) -> LoadedToolGroup:
    """Load plugin tools and registration system messages from a directory."""
    if not directory.is_dir():
        return LoadedToolGroup(tools=[], system_messages=[])
    loaded: list[LoadedTool] = []
    system_messages: list[LoadedSystemMessage] = []
    for path in _iter_tool_module_paths(directory):
        try:
            module = _load_tool_module(path, source_kind=source_kind)
            register_tools = getattr(module, "register_tools", None)
            if callable(register_tools):
                group = _call_register_tools(
                    cast(RegisterToolsFunc, register_tools),
                    context=context,
                    path=path,
                    source_kind=source_kind,
                )
            else:
                group = _discover_module_tools(
                    module,
                    context=context,
                    path=path,
                    source_kind=source_kind,
                )
        except Exception as exc:
            LOGGER.warning("Skipping tool plugin %s: %s", path, exc)
            continue
        loaded.extend(group.tools)
        system_messages.extend(group.system_messages)
    return LoadedToolGroup(tools=loaded, system_messages=system_messages)


def _tool_scope_label(source_kind: ToolSourceKind) -> str:
    return "global ~/.yoke" if source_kind == "global" else "repo .yoke"


def _iter_tool_module_paths(directory: Path) -> Iterable[Path]:
    """Yield only documented plugin locations under one ``.yoke`` directory."""
    paths: list[Path] = []

    try:
        direct_entries = list(directory.iterdir())
    except OSError:
        direct_entries = []
    for path in direct_entries:
        if path.is_file() and _is_tool_module_name(path.name):
            paths.append(path)

    tools_directory = directory / "tools"
    if not tools_directory.is_dir():
        yield from sorted(paths)
        return

    for current, directory_names, file_names in os.walk(tools_directory):
        directory_names[:] = sorted(
            name for name in directory_names if name != "__pycache__"
        )
        current_path = Path(current)
        for file_name in file_names:
            if _is_tool_module_name(file_name):
                paths.append(current_path / file_name)
    yield from sorted(paths)


def _is_tool_module_name(file_name: str) -> bool:
    normalized_name = os.path.normcase(file_name)
    return (
        normalized_name.endswith(".py")
        and normalized_name != "__init__.py"
        and not normalized_name.startswith("_")
    )


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
        sys.modules.pop(module_name, None)
        raise ValueError(
            f"Could not load {_tool_scope_label(source_kind)} "
            f"tool plugin `{path}`. The Python module failed to import: {exc}"
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
    context: ToolRegistrationContext,
    path: Path,
    source_kind: ToolSourceKind,
) -> LoadedToolGroup:
    registration_id = f"{source_kind}:{path}"
    try:
        registration = normalize_tool_registration(register_tools(context))
    except Exception as exc:
        raise ValueError(
            f"Could not register tools from {_tool_scope_label(source_kind)} "
            f"plugin `{path}`. `register_tools(context)` raised: {exc}"
        ) from exc
    tool_list = _validate_tool_list(registration.tools, path=path)
    _bind_runtime_context(context, tool_list)
    return LoadedToolGroup(
        tools=[
            _loaded(
                tool,
                source_kind=source_kind,
                path=path,
                registration_id=registration_id,
            )
            for tool in tool_list
        ],
        system_messages=[
            LoadedSystemMessage(
                message=message,
                source_kind=source_kind,
                source_label=registration_id,
                source_path=path,
                registration_id=registration_id,
            )
            for message in registration.system_messages
        ],
    )


def _discover_module_tools(
    module: ModuleType,
    *,
    context: ToolRegistrationContext,
    path: Path,
    source_kind: ToolSourceKind,
) -> LoadedToolGroup:
    tools: list[LocalTool] = []
    for value in module.__dict__.values():
        if not isinstance(value, type):
            continue
        if not issubclass(value, LocalTool) or value is LocalTool:
            continue
        if value.__module__ != module.__name__ or not value.is_yoke_tool:
            continue
        try:
            tool = value.bind(
                root=context.root,
                home=context.home,
                cancel_requested=context.cancel_requested,
            )
        except Exception as exc:
            raise ValueError(
                f"Could not initialize tool `{value.__name__}` from "
                f"{_tool_scope_label(source_kind)} plugin `{path}`: {exc}"
            ) from exc
        tools.append(tool)
    _bind_runtime_context(context, tools)
    return LoadedToolGroup(
        tools=[_loaded(tool, source_kind=source_kind, path=path) for tool in tools],
        system_messages=[],
    )


def _validate_tool_list(tools: Iterable[LocalTool], *, path: Path) -> list[LocalTool]:
    try:
        tool_list = list(tools)
    except TypeError as exc:
        raise ValueError(
            f"Tool plugin `{path}` is invalid. `register_tools(context)` "
            "must return an iterable of yoke tools."
        ) from exc
    invalid = [tool for tool in tool_list if not isinstance(tool, LocalTool)]
    if invalid:
        raise ValueError(
            f"Tool plugin `{path}` is invalid. `register_tools(context)` "
            "returned objects that are not yoke tools."
        )
    return tool_list


def _bind_runtime_context(
    context: ToolRegistrationContext,
    tools: list[LocalTool],
) -> None:
    runtime_context = ToolRuntimeContext(
        root=context.root,
        home=context.home,
        provider=context.provider,
        model=context.model,
        cancel_requested=context.cancel_requested,
    )
    for tool in tools:
        tool.bind_runtime_context(runtime_context)


def _loaded(
    tool: LocalTool,
    *,
    source_kind: ToolSourceKind,
    path: Path,
    registration_id: str | None = None,
) -> LoadedTool:
    return LoadedTool(
        tool=tool,
        source_kind=source_kind,
        source_label=f"{source_kind}:{path}",
        source_path=path,
        registration_id=registration_id,
    )
