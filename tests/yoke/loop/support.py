from __future__ import annotations

import json
import multiprocessing
import os  # noqa: F401
import time  # noqa: F401

import pytest  # noqa: F401
from pathlib import Path
from threading import Event
from threading import Thread
from pydantic import Field

from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.context import CompactionPolicy, ContextManager  # noqa: F401
from yoke.agent.loop import (
    AfterToolCallContext,  # noqa: F401
    BeforeToolCallContext,  # noqa: F401
    ConversationEntryHistory,  # noqa: F401
    INTERRUPTED_TURN_NOTICE,
    MessageHistory,  # noqa: F401
    RuntimeAgent,  # noqa: F401
)
from yoke.agent.models import (
    Message,
    MessageLocalImageContentPart,  # noqa: F401
    MessageTextContentPart,  # noqa: F401
    ToolCall,
    ToolFunction,
)
from yoke.agent.skills.models import ActiveSkill, SkillSpec  # noqa: F401
from yoke.agent.tools import (
    COMMAND_TOOL_NAME,
    CommandTool,
    EditTool,
    LocalTool,
    ReadTool,
    SubagentTool,
)
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError


def tools(tmp_path: Path):
    return [
        ReadTool.bind(root=tmp_path),
        CommandTool.bind(root=tmp_path),
        EditTool.bind(root=tmp_path),
    ]


class FakeProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls += 1
        if self.calls == 1:
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="edit",
                            arguments='{"path":"hello.txt","new_text":"hello"}',
                        ),
                    )
                ],
            )
        assert messages[-1].role == "tool"
        return Message.assistant("done")


class HistoryProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        assert [message.role for message in messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert messages[0].content == "system prompt"
        assert messages[-2].content == "previous answer"
        return Message.assistant("continued")


class TransformProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        assert [message.role for message in messages] == ["system", "user"]
        assert messages[0].content == "transformed system"
        return Message.assistant("done")


class ParallelProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self, *, tool_name: str = COMMAND_TOOL_NAME) -> None:
        self.calls = 0
        self.tool_name = tool_name

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls += 1
        if self.calls == 1:
            first_command = "sleep 0.2 && echo first"
            second_command = "sleep 0.2 && echo second"
            first_arguments = (
                {"label": "first"}
                if self.tool_name == "barrier"
                else {"command": first_command}
            )
            second_arguments = (
                {"label": "second"}
                if self.tool_name == "barrier"
                else {"command": second_command}
            )
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name=self.tool_name,
                            arguments=json.dumps(first_arguments),
                        ),
                    ),
                    ToolCall(
                        id="call-2",
                        function=ToolFunction(
                            name=self.tool_name,
                            arguments=json.dumps(second_arguments),
                        ),
                    ),
                ],
            )
        return Message.assistant("done")


class SubagentProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self) -> None:
        self.calls = 0
        self.nested_tool_names: set[str] = set()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls += 1
        if self.calls == 1:
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="subagent",
                            arguments=json.dumps(
                                {
                                    "prompt": "Read notes.txt and summarize it.",
                                    "agent_type": "researcher",
                                }
                            ),
                        ),
                    )
                ],
            )
        if self.calls == 2:
            assert tools
            self.nested_tool_names = {
                str(tool["function"]["name"])
                for tool in tools
                if isinstance(tool.get("function"), dict)
            }
            return Message.assistant(
                '{"success": true, "response": "nested summary", "pointers": ["notes.txt"]}'
            )
        assert messages[-1].role == "tool"
        assert "nested summary" in messages[-1].text_content()
        return Message.assistant("done")


class BarrierTool(LocalTool):
    name = "barrier"
    description = "Wait until both test tool calls are executing."

    label: str = Field(description="Tool call label")

    def execute(self) -> dict[str, object]:
        barrier = self._context.get("barrier")
        assert barrier is not None
        barrier.wait()
        return {"ok": True, "label": self.label}


class ContextKeysTool(LocalTool):
    name = "context_keys"
    description = "Return tool context keys for process-spawn tests."

    def execute(self) -> dict[str, object]:
        return {
            "ok": True,
            "keys": sorted(self._context),
            "value": self._context.get("value"),
            "cancelled": self._is_cancel_requested(),
        }


class StoppingProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self, stop_event: Event) -> None:
        self.stop_event = stop_event

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.stop_event.set()
        return Message.assistant("should not be returned")


class OverflowRetryProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        self.calls += 1
        if (
            len(messages) == 2
            and messages[0].role == "system"
            and messages[0].content == COMPACTION_SUMMARY_PROMPT
        ):
            return Message.assistant("Summarized older context")
        if self.calls == 1:
            raise ProviderError("provider does not allow more than 50img")
        assert messages[-1].role == "user"
        return Message.assistant("recovered")


class TokenCountOverflowRetryProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        self.calls += 1
        if (
            len(messages) == 2
            and messages[0].role == "system"
            and messages[0].content == COMPACTION_SUMMARY_PROMPT
        ):
            return Message.assistant("Summarized older context")
        if self.calls == 1:
            raise ProviderError(
                "Provider request failed: prompt token count of 285458 "
                "exceeds the limit of 272000"
            )
        assert any(
            "Summarized older context" in (message.content or "")
            for message in messages
        )
        assert messages[-1].role == "user"
        return Message.assistant("recovered")


class StoppableToolProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls += 1
        if self.calls == 1:
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="read",
                            arguments=json.dumps({"path": "notes.txt"}),
                        ),
                    )
                ],
            )
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
        ]
        assert messages[-2].content == INTERRUPTED_TURN_NOTICE
        assert messages[-1].content == "Use config.py instead"
        return Message.assistant("corrected")
