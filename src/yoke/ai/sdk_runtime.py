"""Shared SDK helpers for constructing runtime agents."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.capabilities import BaseCapability
from yoke.agent.capabilities import ExplicitToolsCapability
from yoke.agent.capabilities import RegisterToolsCapability
from yoke.agent.capabilities import instantiate_capabilities
from yoke.agent.models import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yoke.agent.capabilities import CapabilityInput
    from yoke.agent.tools import LocalTool
    from yoke.agent.tools import RegisterTools

    type AgentTool = LocalTool | type[LocalTool]
else:
    type AgentTool = object
    type CapabilityInput = object


def build_agent_capabilities(
    *,
    capabilities: Sequence[CapabilityInput] | None,
    tools: Sequence[AgentTool],
    register_tools: RegisterTools | None,
) -> tuple[BaseCapability, ...]:
    """Build SDK capabilities while preserving legacy tool configuration."""
    resolved: list[BaseCapability] = []
    if capabilities is not None:
        resolved.extend(instantiate_capabilities(capabilities))
    if tools:
        resolved.append(ExplicitToolsCapability(tools))
    if register_tools is not None:
        resolved.append(RegisterToolsCapability(register_tools))
    return tuple(resolved)


def bind_agent_tools(
    tools: Sequence[AgentTool],
    *,
    context,
    register_tools: RegisterTools | None = None,
) -> object:
    """Bind legacy SDK tools for callers that still use this helper."""
    from yoke.agent.capabilities import CapabilityContext
    from yoke.agent.tools import ToolRegistrationResult

    capability_context = CapabilityContext.from_tool_registration(context)
    registrations = [
        capability.register(capability_context)
        for capability in build_agent_capabilities(
            capabilities=None,
            tools=tools,
            register_tools=register_tools,
        )
    ]
    return ToolRegistrationResult(
        tools=tuple(
            tool for registration in registrations for tool in registration.tools
        ),
        system_messages=tuple(
            message
            for registration in registrations
            for message in registration.system_messages
        ),
    )


def build_system_messages(
    *,
    root: Path,
    sys_prompt: str | None,
    include_agents_file: bool,
) -> list[Message]:
    """Build runtime system messages from SDK configuration."""
    messages: list[Message] = []
    if sys_prompt:
        messages.append(Message.system(sys_prompt))
    if include_agents_file:
        messages.extend(load_agents_messages(root, home=Path.home()))
    return messages


def load_agents_messages(root: Path, *, home: Path) -> list[Message]:
    """Load AGENTS.md messages for a workspace root."""
    from yoke.cli.bootstrap.agents import load_agents_messages as impl

    return impl(root, home=home)
