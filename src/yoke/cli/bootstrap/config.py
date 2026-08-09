"""Bootstrap config resolution for yoke CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from yoke.agent.models import Message
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import never_cancel
from yoke.agent.tools.context import resolve_model_identity
from yoke.agent.instructions import build_system_messages
from yoke.ai.providers.base import Provider
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
from yoke.cli.tools.policy import unmatched_capability_ids
from yoke.cli.tools.policy import unmatched_tool_names


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
    provider: Provider | None = None,
) -> ResolvedAgentConfig:
    """Resolve system messages and enabled tools for the active root."""
    resolved_root = root.resolve()
    resolved_home = (home or Path.home()).resolve()
    resolved_provider = provider or ToolDiscoveryProvider()
    registration_context = ToolRegistrationContext(
        root=resolved_root,
        home=resolved_home,
        provider=resolved_provider,
        model=resolve_model_identity(resolved_provider),
        cancel_requested=cancel_requested or never_cancel,
    )
    discovered_group = load_tools(
        root=resolved_root,
        home=resolved_home,
        include_repo_tools=include_repo_tools,
        include_global_tools=include_global_tools,
        context=registration_context,
    )
    workspace_config = load_effective_workspace_config(
        root=resolved_root,
        home=resolved_home,
        include_workspace_config=include_workspace_config,
    )
    overridden_tools = resolve_tool_overrides(discovered_group.tools)
    active_tools = [
        entry
        for entry in overridden_tools
        if is_tool_allowed(
            entry.tool.name,
            workspace_config.config,
            capability_id=entry.capability_id,
        )
    ]
    denied_tools = [
        entry
        for entry in overridden_tools
        if not is_tool_allowed(
            entry.tool.name,
            workspace_config.config,
            capability_id=entry.capability_id,
        )
    ]
    tool_report = ToolLoadReport(
        discovered_tools=list(discovered_group.tools),
        active_tools=active_tools,
        denied_tools=denied_tools,
        config_path=workspace_config.path,
        unmatched_tool_names=unmatched_tool_names(
            workspace_config.config,
            {entry.tool.name for entry in overridden_tools},
        ),
        unmatched_capability_ids=unmatched_capability_ids(workspace_config.config),
        system_messages=list(discovered_group.system_messages),
    )
    active_registration_ids = {
        entry.registration_id
        for entry in active_tools
        if entry.registration_id is not None
    }
    active_system_messages = [
        entry.message
        for entry in discovered_group.system_messages
        if entry.registration_id is not None
        and entry.registration_id in active_registration_ids
    ]
    return ResolvedAgentConfig(
        system_messages=build_system_messages(
            root=resolved_root,
            base_system_prompt=base_system_prompt,
            include_agents_file=include_agents_file,
        ),
        tools=[entry.tool for entry in active_tools],
        tool_report=tool_report,
        tool_system_messages=active_system_messages,
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


class ToolDiscoveryProvider:
    """Non-executable provider used by provider-less discovery commands."""

    provider_name = "unavailable"
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        """Reject completions because discovery has no active provider."""
        del messages, tools
        raise RuntimeError("No provider is active during tool discovery")
