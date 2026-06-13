"""Availability-based registration for workspace search tools."""

from __future__ import annotations

import shutil

from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.context import ToolRegistrationContext
from yoke.agent.tools.rg import RipgrepTool
from yoke.agent.tools.search import FindTool
from yoke.agent.tools.search import GrepTool
from yoke.agent.tools.search import LsTool


def register_search_tools(context: ToolRegistrationContext) -> list[LocalTool]:
    """Register ripgrep when available, otherwise the Python fallback tools."""
    bind_context: dict[str, object] = {
        "root": context.root,
        "home": context.home,
        "provider": context.provider,
        "cancel_requested": context.cancel_requested,
    }
    if shutil.which("rg") is not None:
        return [RipgrepTool.bind(**bind_context)]
    return [
        GrepTool.bind(**bind_context),
        FindTool.bind(**bind_context),
        LsTool.bind(**bind_context),
    ]
