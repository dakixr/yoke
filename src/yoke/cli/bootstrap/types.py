"""Shared data types for yoke CLI bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yoke.agent.models import Message
from yoke.agent.tools import LocalTool

ToolSourceKind = Literal["default", "global", "repo"]


@dataclass(slots=True)
class ResolvedAgentConfig:
    """Resolved system messages and tools for an agent."""

    system_messages: list[Message]
    tools: list[LocalTool]
    tool_report: ToolLoadReport
    tool_system_messages: list[Message] | None = None


@dataclass(slots=True, frozen=True)
class LoadedTool:
    """One loaded tool with its source metadata."""

    tool: LocalTool
    source_kind: ToolSourceKind
    source_label: str
    source_path: Path | None = None
    capability_id: str | None = None
    registration_id: str | None = None


@dataclass(slots=True, frozen=True)
class LoadedToolGroup:
    """Tools and system messages loaded from one source group."""

    tools: list[LoadedTool]
    system_messages: list[LoadedSystemMessage]


@dataclass(slots=True, frozen=True)
class LoadedSystemMessage:
    """One system message contributed by a loaded registration."""

    message: Message
    source_kind: ToolSourceKind
    source_label: str
    source_path: Path | None = None
    registration_id: str | None = None


@dataclass(slots=True)
class ToolLoadReport:
    """Tool discovery and filtering summary."""

    discovered_tools: list[LoadedTool]
    active_tools: list[LoadedTool]
    denied_tools: list[LoadedTool]
    config_path: Path | None = None
    unmatched_tool_names: list[str] | None = None
    unmatched_capability_ids: list[str] | None = None
    system_messages: list[LoadedSystemMessage] | None = None

    def count(self, source_kind: ToolSourceKind) -> int:
        """Count active tools by source kind."""
        return sum(1 for tool in self.active_tools if tool.source_kind == source_kind)
