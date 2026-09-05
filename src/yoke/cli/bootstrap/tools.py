"""Tool discovery helpers for yoke CLI bootstrap."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.capabilities import create_builtin_capabilities
from yoke.agent.tools import ToolRegistrationContext
from yoke.cli.bootstrap.plugin_loader import load_tools_from_directory
from yoke.cli.bootstrap.types import LoadedTool
from yoke.cli.bootstrap.types import LoadedToolGroup
from yoke.cli.bootstrap.types import LoadedSystemMessage
from yoke.cli.bootstrap.types import ToolSourceKind


def load_tools(
    *,
    root: Path,
    home: Path,
    include_repo_tools: bool,
    include_global_tools: bool,
    context: ToolRegistrationContext,
) -> LoadedToolGroup:
    """Load built-in and plugin tools."""
    capabilities = create_builtin_capabilities(context)
    loaded_tools = [
        LoadedTool(
            tool=tool,
            source_kind="default",
            source_label="default:builtin",
            capability_id=capability.capability_id,
            registration_id=f"capability:{capability.capability_id}",
        )
        for capability in capabilities
        for tool in capability.tools
    ]
    system_messages = [
        LoadedSystemMessage(
            message=message,
            source_kind="default",
            source_label="default:builtin",
            registration_id=f"capability:{capability.capability_id}",
        )
        for capability in capabilities
        for message in capability.system_messages
    ]
    global_directory = home / ".yoke"
    repo_directory = root / ".yoke"
    if include_global_tools:
        group = load_tools_from_directory(
            global_directory,
            context,
            source_kind="global",
        )
        loaded_tools.extend(group.tools)
        system_messages.extend(group.system_messages)
    if include_repo_tools and (
        not include_global_tools or repo_directory != global_directory
    ):
        group = load_tools_from_directory(
            repo_directory,
            context,
            source_kind="repo",
        )
        loaded_tools.extend(group.tools)
        system_messages.extend(group.system_messages)
    return LoadedToolGroup(tools=loaded_tools, system_messages=system_messages)


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
