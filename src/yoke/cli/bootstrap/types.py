"""Shared data types for yoke CLI bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Literal

from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.agent.tools import RegisterTools
from yoke.agent.tools import ToolRegistrationContext

ToolSourceKind = Literal["default", "global", "repo"]
RegisterToolsFunc = RegisterTools
ToolPluginContext = ToolRegistrationContext


@dataclass(slots=True)
class ResolvedAgentConfig:
    """Resolved system messages and tools for an agent."""

    system_messages: list[Message]
    tools: list[LocalTool]
    tool_report: ToolLoadReport
    tool_system_messages: list[Message]


@dataclass(slots=True, frozen=True)
class LoadedTool:
    """One loaded tool with its source metadata."""

    tool: LocalTool
    source_kind: ToolSourceKind
    source_label: str
    source_path: Path | None = None


@dataclass(slots=True, frozen=True)
class LoadedToolContribution:
    """System messages associated with one tool registration."""

    system_messages: tuple[Message, ...]
    tool_names: frozenset[str]
    source_kind: ToolSourceKind
    source_label: str


@dataclass(slots=True)
class ToolDiscoveryResult:
    """Discovered tools and their registration-time prompt contributions."""

    tools: list[LoadedTool]
    contributions: list[LoadedToolContribution]


@dataclass(slots=True)
class ToolLoadReport:
    """Tool discovery and filtering summary."""

    discovered_tools: list[LoadedTool]
    active_tools: list[LoadedTool]
    denied_tools: list[LoadedTool]
    config_path: Path | None = None
    unmatched_config_patterns: list[str] = field(default_factory=list)

    def count(self, source_kind: ToolSourceKind) -> int:
        """Count active tools by source kind."""
        return sum(1 for tool in self.active_tools if tool.source_kind == source_kind)
