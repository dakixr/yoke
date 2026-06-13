# ruff: noqa

from __future__ import annotations
import json
from pathlib import Path
from typing import cast
import httpx
from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools import (
    EditTool,
    ExtractFileContextTool,
    LocalTool,
    LsTool,
    ReadTool,
)
from yoke.ai import (
    Agent,
    CompactionPolicy,
    Context,
    MessageHistory,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    RunConfig,
    Skill,
    complete,
)
from yoke.ai.providers.base import Provider


def tool_function_payload(tool: dict[str, object]) -> dict[str, object] | None:
    """Return the function payload from a tool definition."""
    payload = tool.get("function")
    if not isinstance(payload, dict):
        return None
    if not all(isinstance(key, str) for key in payload):
        return None
    return cast(dict[str, object], payload)


class StaticProvider(Provider):
    def __init__(self, message: Message) -> None:
        self.message = message
        self.calls: list[list[Message]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls.append(messages)
        return self.message
