"""Tool discovery helpers for yoke CLI bootstrap."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import cast

from yoke.agent.capabilities import CapabilityContext
from yoke.agent.capabilities import CapabilityResolver
from yoke.agent.capabilities import default_capabilities
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.agent.tools.context import normalize_tool_registration
from yoke.cli.bootstrap.types import LoadedTool
from yoke.cli.bootstrap.types import LoadedToolContribution
from yoke.cli.bootstrap.types import RegisterToolsFunc
from yoke.cli.bootstrap.types import ToolDiscoveryResult
from yoke.cli.bootstrap.types import ToolLoadFailure
from yoke.cli.bootstrap.types import ToolSourceKind


def _tool_scope_label(source_kind: ToolSourceKind) -> str:
    return "global ~/.yoke" if source_kind == "global" else "repo .yoke"


def load_tools(
    *,
    root: Path,
    home: Path,
    include_repo_tools: bool,
    include_global_tools: bool,
    context: ToolRegistrationContext,
) -> ToolDiscoveryResult:
    """Load built-in and plugin tools."""
    resolution = CapabilityResolver(default_capabilities()).resolve(
        CapabilityContext.from_tool_registration(context)
    )
    builtin_tools = list(resolution.tools)
    loaded_tools: list[LoadedTool] = [
        LoadedTool(
            tool=tool,
            source_kind="default",
            source_label=f"default:{capability.name}",
            capability_name=capability.name,
        )
        for capability, registration in resolution.registrations
        for tool in registration.tools
    ]
    builtin_messages = tuple(
        message.model_copy(deep=True) for message in resolution.system_messages
    )
    failures: list[ToolLoadFailure] = []
    contributions = (
        [
            LoadedToolContribution(
                system_messages=builtin_messages,
                tool_names=frozenset(
                    tool.name
                    for tool in builtin_tools
                    if tool.name in {"apply_patch", "edit", "write"}
                ),
                source_kind="default",
                source_label="default:file.edit",
            )
        ]
        if builtin_messages
        else []
    )
    if include_global_tools:
        discovered = _load_tools_from_directory(
            home / ".yoke" / "tools",
            context,
            source_kind="global",
        )
        loaded_tools.extend(discovered.tools)
        contributions.extend(discovered.contributions)
        failures.extend(discovered.failures)
    if include_repo_tools:
        discovered = _load_tools_from_directory(
            root / ".yoke" / "tools",
            context,
            source_kind="repo",
        )
        loaded_tools.extend(discovered.tools)
        contributions.extend(discovered.contributions)
        failures.extend(discovered.failures)
    return ToolDiscoveryResult(
        tools=loaded_tools,
        contributions=contributions,
        failures=failures,
    )


def create_builtin_tools(context: ToolRegistrationContext) -> list[LocalTool]:
    """Create the default built-in tool set."""
    return list(_register_builtin_tools(context).tools)


def _register_builtin_tools(
    context: ToolRegistrationContext,
) -> ToolRegistrationResult:
    capability_context = CapabilityContext.from_tool_registration(context)
    resolution = CapabilityResolver(default_capabilities()).resolve(capability_context)
    tools = tuple(
        tool
        for _capability, registration in resolution.registrations
        for tool in registration.tools
    )
    system_messages = tuple(
        message.model_copy(deep=True)
        for _capability, registration in resolution.registrations
        for message in registration.system_messages
    )
    return ToolRegistrationResult(tools=tools, system_messages=system_messages)


def _register_builtin_capabilities(
    context: ToolRegistrationContext,
) -> dict[str, tuple[LocalTool, ...]]:
    resolution = CapabilityResolver(default_capabilities()).resolve(
        CapabilityContext.from_tool_registration(context)
    )
    return {
        capability.name: registration.tools
        for capability, registration in resolution.registrations
    }


def _load_tools_from_directory(
    directory: Path,
    context: ToolRegistrationContext,
    *,
    source_kind: ToolSourceKind,
) -> ToolDiscoveryResult:
    if not directory.is_dir():
        return ToolDiscoveryResult(tools=[], contributions=[])
    loaded: list[LoadedTool] = []
    contributions: list[LoadedToolContribution] = []
    failures: list[ToolLoadFailure] = []
    for path in _iter_tool_module_paths(directory):
        try:
            module = _load_tool_module(path, source_kind=source_kind)
            register_tools = getattr(module, "register_tools", None)
            if callable(register_tools):
                registration = _call_register_tools(
                    cast(RegisterToolsFunc, register_tools),
                    context=context,
                    path=path,
                    source_kind=source_kind,
                )
                tools = list(registration.tools)
            else:
                tools = _discover_module_tools(
                    module,
                    context=context,
                    path=path,
                    source_kind=source_kind,
                )
                registration = ToolRegistrationResult(tools=tools)
        except Exception as exc:
            sys.modules.pop(_tool_module_name(path), None)
            failures.append(
                ToolLoadFailure(
                    source_kind=source_kind,
                    source_path=path,
                    error=str(exc),
                )
            )
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
        contributions.extend(
            _registration_contributions(
                registration,
                source_kind=source_kind,
                source_label=f"{source_kind}:{path}",
            )
        )
    return ToolDiscoveryResult(
        tools=loaded,
        contributions=contributions,
        failures=failures,
    )


def _iter_tool_module_paths(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        yield path


def _load_tool_module(path: Path, *, source_kind: ToolSourceKind) -> ModuleType:
    module_name = _tool_module_name(path)
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
            f"tool plugin `{path}`. "
            f"The Python module failed to import: {exc}"
        ) from exc
    return module


def _tool_module_name(path: Path) -> str:
    return f"{_ensure_tool_package(path.parent)}.{path.stem}"


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
) -> ToolRegistrationResult:
    try:
        tools = normalize_tool_registration(register_tools(context))
    except Exception as exc:
        raise ValueError(
            f"Could not register tools from "
            f"{_tool_scope_label(source_kind)} plugin `{path}`. "
            f"`register_tools(context)` raised: {exc}"
        ) from exc
    try:
        tool_list = list(tools.tools)
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
    return ToolRegistrationResult(
        tools=tool_list,
        system_messages=tools.system_messages,
    )


def _registration_contributions(
    registration: ToolRegistrationResult,
    *,
    source_kind: ToolSourceKind,
    source_label: str,
) -> list[LoadedToolContribution]:
    system_messages = tuple(
        message.model_copy(deep=True) for message in registration.system_messages
    )
    if not system_messages:
        return []
    return [
        LoadedToolContribution(
            system_messages=system_messages,
            tool_names=frozenset(tool.name for tool in registration.tools),
            source_kind=source_kind,
            source_label=source_label,
        )
    ]


def _discover_module_tools(
    module: ModuleType,
    *,
    context: ToolRegistrationContext,
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
