"""CLI helpers for inspecting MCP configuration."""

from __future__ import annotations

from pathlib import Path

from yoke.mcp import McpManager


def format_mcp_status(*, root: Path, home: Path, server: str | None = None) -> str:
    """Return MCP status text for CLI output."""
    manager = McpManager.from_paths(root=root, home=home)
    try:
        return manager.status_text(server)
    finally:
        manager.close()
