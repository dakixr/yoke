# ruff: noqa

from __future__ import annotations
from pathlib import Path
from typing import Any, cast
import pytest
from yoke.agent.models import Message
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai.providers.base import Provider
from yoke.cli.bootstrap.config import resolve_agent_config
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config import build_tool_report


class StaticProvider(Provider):
    def __init__(self, message: Message) -> None:
        self.message = message
        self.calls: list[list[Message]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls.append(messages)
        return self.message


def definition_names(agent: Any) -> list[str]:
    runtime_agent = getattr(agent, "_runtime", agent)
    definitions = cast(
        list[dict[str, Any]],
        [tool.to_definition() for tool in runtime_agent.tools.values()],
    )
    return [tool["function"]["name"] for tool in definitions]


def report_names(report: ToolLoadReport) -> list[str]:
    return [entry.tool.name for entry in report.active_tools]


def execute_tool(
    tools: list[Any], name: str, arguments: dict[str, object]
) -> dict[str, object]:
    for tool in tools:
        if tool.name == name:
            return cast(dict[str, object], tool.parse_arguments(arguments).execute())
    return {"ok": False, "error": f"Unknown tool: {name}"}
