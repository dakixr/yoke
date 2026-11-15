"""Shared data types for yoke CLI bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.agent.tools.registration import LoadedSystemMessage as LoadedSystemMessage
from yoke.agent.tools.registration import LoadedTool as LoadedTool
from yoke.agent.tools.registration import ToolLoadReport as ToolLoadReport
from yoke.agent.tools.registration import ToolSourceKind as ToolSourceKind


@dataclass(slots=True)
class ResolvedAgentConfig:
    """Resolved system messages and tools for an agent."""

    system_messages: list[Message]
    tools: list[LocalTool]
    tool_report: ToolLoadReport
    tool_system_messages: list[Message] | None = None


@dataclass(slots=True, frozen=True)
class LoadedToolGroup:
    """Tools and system messages loaded from one source group."""

    tools: list[LoadedTool]
    system_messages: list[LoadedSystemMessage]
