"""Bootstrap config resolution for yoke CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from yoke.cli.bootstrap.agents import build_system_messages
from yoke.cli.bootstrap.tools import load_tools
from yoke.cli.bootstrap.tools import resolve_tool_overrides
from yoke.cli.bootstrap.types import ResolvedAgentConfig
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.tools.policy import LoadedWorkspaceConfig
from yoke.cli.tools.policy import default_yoke_config
from yoke.cli.tools.policy import is_tool_allowed
from yoke.cli.tools.policy import load_global_config
from yoke.cli.tools.policy import load_workspace_config
from yoke.cli.tools.policy import merge_configs
from yoke.cli.tools.policy import unmatched_tool_patterns


def resolve_agent_config(
    *,
    root: Path,
    base_system_prompt: str | None = None,
    include_repo_tools: bool = True,
    include_global_tools: bool = True,
    include_agents_file: bool = True,
    home: Path | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    include_workspace_config: bool = True,
) -> ResolvedAgentConfig:
    """Resolve system messages and enabled tools for the active root."""
    resolved_root = root.resolve()
    resolved_home = (home or Path.home()).resolve()
    discovered_tools = load_tools(
        root=resolved_root,
        home=resolved_home,
        include_repo_tools=include_repo_tools,
        include_global_tools=include_global_tools,
        cancel_requested=cancel_requested,
    )
    workspace_config = load_effective_workspace_config(
        root=resolved_root,
        home=resolved_home,
        include_workspace_config=include_workspace_config,
    )
    overridden_tools = resolve_tool_overrides(discovered_tools)
    active_tools = [
        entry
        for entry in overridden_tools
        if is_tool_allowed(entry.tool.name, workspace_config.config)
    ]
    denied_tools = [
        entry
        for entry in overridden_tools
        if not is_tool_allowed(entry.tool.name, workspace_config.config)
    ]
    tool_report = ToolLoadReport(
        discovered_tools=list(discovered_tools),
        active_tools=active_tools,
        denied_tools=denied_tools,
        config_path=workspace_config.path,
        unmatched_config_patterns=unmatched_tool_patterns(
            workspace_config.config,
            {entry.tool.name for entry in overridden_tools},
        ),
    )
    return ResolvedAgentConfig(
        system_messages=build_system_messages(
            root=resolved_root,
            base_system_prompt=base_system_prompt,
            include_agents_file=include_agents_file,
        ),
        tools=[entry.tool for entry in active_tools],
        tool_report=tool_report,
    )


def load_effective_workspace_config(
    *,
    root: Path,
    home: Path | None = None,
    include_workspace_config: bool = True,
) -> LoadedWorkspaceConfig:
    """Load the merged default/global/repo workspace config."""
    resolved_home = (home or Path.home()).resolve()
    if include_workspace_config:
        default_config = default_yoke_config()
        global_config = load_global_config(resolved_home)
        repo_config = load_workspace_config(root)
        return LoadedWorkspaceConfig(
            path=repo_config.path or global_config.path,
            config=merge_configs(
                default_config,
                global_config.config,
                repo_config.config,
            ),
        )
    return LoadedWorkspaceConfig(path=None, config=default_yoke_config())
