"""Availability-based registration for workspace search tools."""

from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.context import ToolRegistrationContext


def register_search_tools(context: ToolRegistrationContext) -> list[LocalTool]:
    """Register ripgrep when available, otherwise the Python fallback tools."""
    from yoke.agent.capabilities.builtins import FileSearchCapability

    registration = FileSearchCapability().resolve(context)
    return list(registration.tools)
