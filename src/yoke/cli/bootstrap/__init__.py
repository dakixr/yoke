"""Bootstrap package exports for yoke CLI."""

from __future__ import annotations

from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.cli.bootstrap.config import load_effective_workspace_config
    from yoke.cli.bootstrap.config import resolve_agent_config
    from yoke.cli.bootstrap.types import LoadedTool
    from yoke.cli.bootstrap.types import RegisterToolsFunc
    from yoke.cli.bootstrap.types import ResolvedAgentConfig
    from yoke.cli.bootstrap.types import ToolLoadReport
    from yoke.cli.bootstrap.types import ToolPluginContext
    from yoke.cli.bootstrap.types import ToolSourceKind

__all__ = [
    "LoadedTool",
    "RegisterToolsFunc",
    "ResolvedAgentConfig",
    "ToolLoadReport",
    "ToolPluginContext",
    "ToolSourceKind",
    "load_effective_workspace_config",
    "resolve_agent_config",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve bootstrap exports without importing config eagerly."""
    if name in {"load_effective_workspace_config", "resolve_agent_config"}:
        from yoke.cli.bootstrap import config

        return getattr(config, name)
    if name in {
        "LoadedTool",
        "RegisterToolsFunc",
        "ResolvedAgentConfig",
        "ToolLoadReport",
        "ToolPluginContext",
        "ToolSourceKind",
    }:
        from yoke.cli.bootstrap import types

        return getattr(types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
