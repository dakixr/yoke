"""Availability-based registration for workspace search tools."""

from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.context import ToolRegistrationContext


def register_search_tools(context: ToolRegistrationContext) -> list[LocalTool]:
    """Register ripgrep when available, otherwise the Python fallback tools."""
    from yoke.agent.capabilities.builtin import FileSearchCapability
    from yoke.agent.capabilities.core import CapabilityContext

    registration = FileSearchCapability().register(
        CapabilityContext.from_tool_registration(context)
    )
    return list(registration.tools)
