# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from yoke.agent.models import Message
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.tools import EditTool
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.ai import Agent
from yoke.ai import Image
from yoke.ai import RunConfig
from yoke.ai import Skill
from yoke.ai import StructuredOutputError
from yoke.ai import complete
from yoke.ai.providers.base import Provider


class RecordingProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = None

    def __init__(self, *responses: Message) -> None:
        self.responses = list(responses) or [Message.assistant("done")]
        self.calls: list[tuple[list[Message], list[dict[str, object]]]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls.append(
            (
                [message.model_copy(deep=True) for message in messages],
                list(tools),
            )
        )
        if len(self.calls) <= len(self.responses):
            return self.responses[len(self.calls) - 1]
        return self.responses[-1]


class Summary(BaseModel):
    verdict: str
    risks: list[str]


def test_complete_uses_sys_prompt_images_and_no_tools() -> None:
    provider = RecordingProvider(Message.assistant("done"))

    result = complete(
        provider=provider,
        sys_prompt="Be brief.",
        prompt="Describe [Image #1].",
        images=[Image.from_path("shot.png")],
    )

    assert result.output == "done"
    messages, tools = provider.calls[-1]
    assert tools == []
    assert messages[0] == Message.system("Be brief.")
    assert isinstance(messages[1].content, list)
    assert messages[1].content == [
        MessageTextContentPart(text="Describe [Image #1]."),
        MessageLocalImageContentPart(
            path=str(Path("shot.png").expanduser().resolve()),
            label="[Image #1]",
        ),
    ]


def test_complete_returns_structured_output() -> None:
    provider = RecordingProvider(Message.assistant('{"verdict":"pass","risks":[]}'))

    result = complete(
        provider=provider,
        prompt="Summarize.",
        output_type=Summary,
    )

    assert result.structured == Summary(verdict="pass", risks=[])
    assert result.output == '{"verdict":"pass","risks":[]}'
    prompt = provider.calls[-1][0][-1].text_content() or ""
    assert "JSON Schema:" in prompt
    assert '"verdict"' in prompt
    assert '"risks"' in prompt


def test_complete_structured_output_failure_modes() -> None:
    provider = RecordingProvider(Message.assistant("not json"))

    with pytest.raises(StructuredOutputError) as exc_info:
        complete(
            provider=provider,
            prompt="Summarize.",
            output_type=Summary,
        )

    assert exc_info.value.output == "not json"


def test_public_agent_prompt_is_stateful_and_uses_sys_prompt(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(
        Message.assistant("first"), Message.assistant("second")
    )
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            sys_prompt="You are concise.",
            tools=[],
            include_agents_file=False,
        ),
    )

    first = agent.prompt("first")
    second = agent.prompt("second")

    assert first.output == "first"
    assert second.output == "second"
    second_messages, second_tools = provider.calls[-1]
    assert second_tools == []
    assert [message.role for message in second_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert second_messages[0].content == "You are concise."
    assert second_messages[-1].content == "second"


def test_public_agent_prompt_accepts_images(tmp_path: Path) -> None:
    provider = RecordingProvider(Message.assistant("done"))
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
        ),
    )

    result = agent.prompt(
        "Inspect [Image #1].",
        images=[Image.from_path("ui.png")],
    )

    assert result.output == "done"
    messages, _tools = provider.calls[-1]
    assert isinstance(messages[-1].content, list)
    assert messages[-1].content == [
        MessageTextContentPart(text="Inspect [Image #1]."),
        MessageLocalImageContentPart(
            path=str(Path("ui.png").expanduser().resolve()),
            label="[Image #1]",
        ),
    ]


def test_public_agent_prompt_adds_structured_output_schema(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(Message.assistant('{"verdict":"pass","risks":[]}'))
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
        ),
    )

    result = agent.prompt("Summarize.", output_type=Summary)

    assert result.structured == Summary(verdict="pass", risks=[])
    messages, _tools = provider.calls[-1]
    prompt = messages[-1].text_content() or ""
    assert "JSON Schema:" in prompt
    assert '"verdict"' in prompt
    assert '"risks"' in prompt


def test_public_agent_prompt_executes_local_tools(tmp_path: Path) -> None:
    provider = RecordingProvider(
        Message(
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
        ),
        Message.assistant("done"),
    )
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[EditTool],
            include_agents_file=False,
        ),
    )

    result = agent.prompt("Create a file.")

    assert result.output == "done"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"


def test_local_tool_public_alias_supports_user_tools() -> None:
    class EchoTool(LocalTool):
        name = "echo"
        description = "Echo text."

        text: str

        def execute(self) -> dict[str, object]:
            return {"ok": True, "text": self.text}

    assert EchoTool.bind().name == "echo"


def test_sdk_tool_context_exposes_provider_and_refreshes_model_metadata(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(Message.assistant("done"))
    provider.provider_name = "Demo"
    provider.config = SimpleNamespace(
        model="model-a",
        reasoning_effort="High",
    )
    registrations: list[tuple[str, str | None, object]] = []

    class InspectContextTool(LocalTool):
        name = "inspect_context"
        description = "Inspect the public tool runtime context."

        def execute(self) -> dict[str, object]:
            return {
                "ok": True,
                "provider": self.context.provider,
                "provider_name": self.context.provider_name,
                "model_key": self.context.model_key,
                "reasoning_effort": self.context.reasoning_effort,
            }

    def register_tools(context: ToolRegistrationContext):
        registrations.append(
            (context.provider_name, context.model_key, context.provider)
        )
        return [InspectContextTool.bind()]

    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            register_tools=register_tools,
            include_agents_file=False,
        ),
    )

    tool = agent._runtime.tools["inspect_context"]
    initial = tool.execute()

    assert registrations == [("demo", "demo:model-a", provider)]
    assert initial == {
        "ok": True,
        "provider": provider,
        "provider_name": "demo",
        "model_key": "demo:model-a",
        "reasoning_effort": "high",
    }

    provider.config.model = "model-b"
    agent.prompt("refresh tools")

    assert registrations[-1] == ("demo", "demo:model-b", provider)
    assert agent._runtime.tools["inspect_context"].context.model_key == (
        "demo:model-b"
    )


def test_sdk_registration_result_contributes_system_messages(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(Message.assistant("done"))

    class PromptTool(LocalTool):
        name = "prompt_tool"
        description = "A tool with model-facing instructions."

        def execute(self) -> dict[str, object]:
            return {"ok": True}

    def register_tools(context: ToolRegistrationContext):
        del context
        return ToolRegistrationResult(
            tools=[PromptTool.bind()],
            system_messages=[Message.system("Use prompt_tool carefully.")],
        )

    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            sys_prompt="Base instructions.",
            register_tools=register_tools,
            include_agents_file=False,
        ),
    )
    agent.prompt("hello")

    messages, _tools = provider.calls[-1]
    assert [message.content for message in messages[:2]] == [
        "Base instructions.",
        "Use prompt_tool carefully.",
    ]


def test_public_agent_renders_inline_skill(tmp_path: Path) -> None:
    provider = RecordingProvider(Message.assistant("done"))
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
            skills=[
                Skill.inline(
                    name="repo-style",
                    sys_prompt="Prefer minimal patches.",
                )
            ],
        ),
    )

    result = agent.prompt("hello")

    assert result.output == "done"
    messages, _tools = provider.calls[-1]
    combined = "\n".join(message.text_content() or "" for message in messages)
    assert "Active skill:" in combined
    assert "repo-style" in combined
    assert "Prefer minimal patches." in combined
