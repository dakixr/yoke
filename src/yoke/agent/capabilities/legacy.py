"""Compatibility capabilities for legacy tool registration APIs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from yoke.agent.capabilities.core import BaseCapability
from yoke.agent.capabilities.core import CapabilityContext
from yoke.agent.capabilities.core import CapabilityRegistration
from yoke.agent.tools.context import RegisterTools
from yoke.agent.tools.context import normalize_tool_registration

if TYPE_CHECKING:
    from yoke.agent.tools.base import LocalTool

    type AgentTool = LocalTool | type[LocalTool]
else:
    type AgentTool = object


class ExplicitToolsCapability(BaseCapability):
    """Capability wrapper for SDK-provided tool classes and instances."""

    name = "tools.explicit"
    description = "Tools explicitly supplied by SDK configuration."

    def __init__(self, tools: Sequence[AgentTool]) -> None:
        self._tools = tuple(tools)

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        from yoke.agent.tools import LocalTool
        from yoke.agent.tools import WorkspaceTool

        bound_tools: list[LocalTool] = []
        for tool in self._tools:
            if isinstance(tool, LocalTool):
                copied = tool.model_copy(deep=False)
                copied._context = dict(tool._context)
                bound_tools.append(copied)
                continue
            if isinstance(tool, type) and issubclass(tool, LocalTool):
                bind_context = (
                    {
                        "root": context.root,
                        "home": context.home,
                        "provider": context.provider,
                    }
                    if issubclass(tool, WorkspaceTool)
                    else {"provider": context.provider}
                )
                bound_tools.append(tool.bind(**bind_context))
                continue
            raise TypeError(
                "Agent tools must be LocalTool instances or LocalTool classes. "
                f"Got {tool!r}."
            )
        return CapabilityRegistration(tools=tuple(bound_tools))


class RegisterToolsCapability(BaseCapability):
    """Capability wrapper for legacy provider-aware registration callbacks."""

    name = "tools.register"
    description = "Tools returned by a legacy RegisterTools callback."

    def __init__(self, register_tools: RegisterTools) -> None:
        self._register_tools = register_tools

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        registration = normalize_tool_registration(
            self._register_tools(context.to_tool_registration())
        )
        return CapabilityRegistration(
            tools=tuple(registration.tools),
            system_messages=tuple(registration.system_messages),
        )


class BuiltinCapabilityIdsCapability(BaseCapability):
    """Resolve SDK string capability IDs through the built-in registry."""

    name = "tools.capability_ids"
    description = "Built-in tools selected by stable SDK capability IDs."

    def __init__(self, capability_ids: Sequence[str]) -> None:
        self._capability_ids = tuple(capability_ids)

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        from yoke.agent.capabilities.builtin import resolve_builtin_capability_id

        registrations = [
            resolve_builtin_capability_id(capability_id, context)
            for capability_id in self._capability_ids
        ]
        tools = []
        seen_names: set[str] = set()
        for registration in registrations:
            for tool in registration.tools:
                if tool.name not in seen_names:
                    tools.append(tool)
                    seen_names.add(tool.name)
        return CapabilityRegistration(
            tools=tuple(tools),
            system_messages=tuple(
                message
                for registration in registrations
                for message in registration.system_messages
            ),
        )
