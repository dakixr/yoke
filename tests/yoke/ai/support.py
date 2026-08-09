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
from yoke.agent.context import CompactionPolicy
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai import complete
from yoke.ai.providers import OpenAICompatibleConfig
from yoke.ai.providers import OpenAICompatibleProvider
from yoke.ai.skills import Skill
from yoke.ai.types import Context
from yoke.ai.providers.base import Provider


class StaticProvider(Provider):
    def __init__(self, message: Message) -> None:
        self.message = message
        self.calls: list[list[Message]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls.append(messages)
        return self.message
