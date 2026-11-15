"""Cancellable searches with a hard capture limit for composed MCP reads."""

from __future__ import annotations

import threading
from typing import Any

from yoke.mcp_server.search import (
    MCPFdTool,
    MCPRipgrepTool,
)


def execute(
    tool: MCPFdTool | MCPRipgrepTool, cancel: threading.Event | None
) -> dict[str, Any]:
    return tool.run(cancel=cancel, capture_limit=4 * 1024 * 1024)
