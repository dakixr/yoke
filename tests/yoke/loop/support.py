from __future__ import annotations

from pathlib import Path

from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.models import (
    Message,
    ToolCall,
    ToolFunction,
)
from yoke.agent.tools import (
    CommandTool,
    EditTool,
    LocalTool,
    ReadTool,
)
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError


class FastWriteTool(LocalTool):
    name = "fast_write"
    description = "Write a text file inside the test workspace."
    execute_in_process = True
    path: str
    text: str

    def execute(self) -> dict[str, object]:
        root = self._context["root"]
        assert isinstance(root, Path)
        target = root / self.path
        target.write_text(self.text, encoding="utf-8")
        return {"ok": True, "path": self.path}


def tools(tmp_path: Path):
    return [
        ReadTool.bind(root=tmp_path),
        CommandTool.bind(root=tmp_path),
        EditTool.bind(root=tmp_path),
        FastWriteTool.bind(root=tmp_path),
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
                            name="fast_write",
                            arguments=('{"path":"hello.txt","text":"hello"}'),
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
        if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
            return Message.assistant("Summarized older context")
        if self.calls == 1:
            raise ProviderError("provider does not allow more than 50img")
        assert messages[-1].role == "assistant"
        return Message.assistant("recovered")
