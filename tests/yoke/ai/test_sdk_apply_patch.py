# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path
from typing import cast

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.tools import ApplyPatchTool
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai.providers.base import Provider

from .support import tool_function_payload


def test_sdk_agent_accepts_raw_apply_patch_tool_arguments(
    tmp_path: Path,
) -> None:
    patch = """*** Begin Patch
*** Add File: hello.txt
+hello
*** End Patch
"""

    class RawApplyPatchProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            self.calls += 1
            if self.calls == 1:
                tool_names = {
                    cast(str, fn["name"])
                    for tool in tools
                    if (fn := tool_function_payload(tool)) is not None
                    and isinstance(fn.get("name"), str)
                }
                assert "apply_patch" in tool_names
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="apply_patch",
                                arguments=patch,
                            ),
                        )
                    ],
                )
            assert messages[-1].role == "tool"
            assert "hello.txt" in (messages[-1].content or "")
            return Message.assistant("done")

    agent = Agent(
        provider=RawApplyPatchProvider(),
        config=RunConfig(
            root=tmp_path,
            tools=[ApplyPatchTool],
            include_agents_file=False,
        ),
    )
    result = agent.prompt("create a file")

    assert result.output == "done"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello\n"
