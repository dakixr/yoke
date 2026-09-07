"""Read-only MCP variants of Yoke's native typed search tools."""

from __future__ import annotations

from yoke.agent.tools.fd import FdTool
from yoke.agent.tools.rg import RipgrepTool


class MCPRipgrepTool(RipgrepTool):
    """Expose typed ripgrep search without local configuration hooks."""

    read_rg_config = False

    description = (
        "Search file contents or list files using typed ripgrep options. Use "
        "patterns, paths, globs, types, context, mode, limit, and sort directly. "
        "Use exec_command for shell pipelines or command execution."
    )


class MCPFdTool(FdTool):
    """Expose typed read-only fd file discovery."""

    description = (
        "Find files and directories using typed fd options such as pattern, "
        "paths, types, extensions, depth, excludes, limit, and sort. Use "
        "exec_command for shell pipelines or command execution."
    )
