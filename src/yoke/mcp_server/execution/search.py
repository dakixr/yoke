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
    # Always take the bounded, cancellable path: without an event the tools
    # fall back to an uncapped subprocess.run.
    return tool.run(
        cancel=cancel if cancel is not None else threading.Event(),
        capture_limit=4 * 1024 * 1024,
    )
