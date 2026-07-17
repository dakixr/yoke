# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.tools import ApplyPatchTool
from yoke.agent.tools import register_write_tool
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai.providers.base import Provider

from .support import tool_function_payload


def test_sdk_write_registration_selects_schema_from_model_id(
    tmp_path: Path,
) -> None:
    class ModelProvider(Provider):
        provider_name = "demo"
        supports_image_inputs = False
        max_images_per_message = None

        def __init__(self, model: str) -> None:
            self.config = SimpleNamespace(model=model, reasoning_effort=None)

        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            del messages, tools
            return Message.assistant("done")

    gpt_agent = Agent(
        provider=ModelProvider("GPT-5.4-mini"),
        config=RunConfig(
            root=tmp_path,
            register_tools=register_write_tool,
            include_agents_file=False,
        ),
    )
    non_gpt_agent = Agent(
        provider=ModelProvider("kimi-k2.7-code"),
        config=RunConfig(
            root=tmp_path,
            register_tools=register_write_tool,
            include_agents_file=False,
        ),
    )

    assert set(gpt_agent._runtime.tools) == {"apply_patch"}
    assert set(non_gpt_agent._runtime.tools) == {"edit", "write"}
    assert "Use the `apply_patch` tool" in (
        gpt_agent._runtime.tools["apply_patch"].description
    )
    assert gpt_agent._runtime.context_manager.instructions == []
    assert non_gpt_agent._runtime.context_manager.instructions == []
    assert "Use oldString/newString" in non_gpt_agent._runtime.tools["edit"].description
    gpt_schema = gpt_agent._runtime.tools["apply_patch"].to_definition()
    non_gpt_schema = non_gpt_agent._runtime.tools["edit"].to_definition()
    gpt_parameters = cast(
        dict[str, dict[str, object]],
        cast(dict[str, object], gpt_schema["function"])["parameters"],
    )
    non_gpt_parameters = cast(
        dict[str, dict[str, object]],
        cast(dict[str, object], non_gpt_schema["function"])["parameters"],
    )
    assert "input" in gpt_parameters["properties"]
    assert "oldString" in non_gpt_parameters["properties"]
    assert "write" in non_gpt_agent._runtime.tools


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
