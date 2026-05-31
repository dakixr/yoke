"""Shared data types for yoke CLI bootstrap."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yoke.agent.models import Message
from yoke.agent.tools import LocalTool

ToolSourceKind = Literal["default", "global", "repo"]
RegisterToolsFunc = Callable[["ToolPluginContext"], Iterable[LocalTool]]


@dataclass(slots=True, frozen=True)
class ToolPluginContext:
    """Context passed to external tool plugins."""

    root: Path
    home: Path
    cancel_requested: Callable[[], bool] | None = None


@dataclass(slots=True)
class ResolvedAgentConfig:
    """Resolved system messages and tools for an agent."""

    system_messages: list[Message]
    tools: list[LocalTool]
    tool_report: ToolLoadReport


@dataclass(slots=True, frozen=True)
class LoadedTool:
    """One loaded tool with its source metadata."""

    tool: LocalTool
    source_kind: ToolSourceKind
    source_label: str
    source_path: Path | None = None


@dataclass(slots=True)
class ToolLoadReport:
    """Tool discovery and filtering summary."""

    discovered_tools: list[LoadedTool]
    active_tools: list[LoadedTool]
    denied_tools: list[LoadedTool]
    config_path: Path | None = None
    unmatched_config_patterns: list[str] | None = None

    def count(self, source_kind: ToolSourceKind) -> int:
        """Count active tools by source kind."""
        return sum(1 for tool in self.active_tools if tool.source_kind == source_kind)
