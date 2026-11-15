"""Ordinary model tool mistakes must not log warnings or tracebacks."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import Provider

from .support import tools as local_tools


class MistakenToolCalls(Provider):
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="unknown",
                        function=ToolFunction(name="nosuch_tool", arguments="{}"),
                    ),
                    ToolCall(
                        id="invalid",
                        function=ToolFunction(name="read", arguments='{"bogus": 1}'),
                    ),
                ],
            )
        return Message.assistant("done")


def test_unknown_tools_and_invalid_arguments_do_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent = RuntimeAgent(provider=MistakenToolCalls(), tools=local_tools(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="yoke"):
        result = agent.run("go")
    assert result.output == "done"
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
