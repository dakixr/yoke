# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from yoke.agent.capabilities import FileSearchCapability
from yoke.agent.models import Message
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.agent.tools import WriteTool
from yoke.ai import Agent
from yoke.ai import AgentStateSnapshot
from yoke.ai import Image
from yoke.ai import RunConfig
from yoke.ai import Skill
from yoke.ai import StructuredOutputError
from yoke.ai import complete
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from tests.yoke.ai.support import tool_function_payload


class RecordingProvider(Provider):
    supports_image_inputs = True
    max_images_per_message = None
    provider_name = "recording"

    def __init__(self, *responses: Message) -> None:
        self.responses = list(responses) or [Message.assistant("done")]
        self.calls: list[tuple[list[Message], list[dict[str, object]]]] = []
        self.config = SimpleNamespace(model="recording", reasoning_effort=None)

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


class ModelPromptProvider(RecordingProvider):
    provider_name = "demo"

    def __init__(self, *responses: Message, model: str = "gpt-demo") -> None:
        super().__init__(*responses)
        self.config = SimpleNamespace(model=model)

    def current_model_id(self) -> str | None:
        return self.config.model

    def current_model_info(self) -> ProviderModelInfo | None:
        if self.config.model == "gpt-demo":
            return ProviderModelInfo(
                id="gpt-demo",
                display_name="GPT Demo",
                context_window_tokens=100_000,
                thinking_levels=("low",),
                system_messages=(Message.system("Use GPT Demo provider steering."),),
            )
        if self.config.model == "kimi-demo":
            return ProviderModelInfo(
                id="kimi-demo",
                display_name="Kimi Demo",
                context_window_tokens=100_000,
                thinking_levels=("low",),
                system_messages=(Message.system("Use Kimi Demo provider steering."),),
            )
        return None

    def list_models(self) -> list[ProviderModelInfo]:
        return [
            model
            for model in [
                self.current_model_info(),
                ProviderModelInfo(
                    id="other-demo",
                    display_name="Other Demo",
                    context_window_tokens=100_000,
                    thinking_levels=("low",),
                ),
            ]
            if model is not None
        ]

    def set_model(
        self,
        model_id: str,
        *,
        reasoning_effort: str | None = None,
    ) -> None:
        del reasoning_effort
        self.config.model = model_id


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


def test_complete_includes_current_provider_model_system_messages() -> None:
    provider = ModelPromptProvider(Message.assistant("done"))

    complete(
        provider=provider,
        sys_prompt="Base instructions.",
        prompt="hello",
    )

    messages, _tools = provider.calls[-1]
    assert [message.content for message in messages[:2]] == [
        "Base instructions.",
        "Use GPT Demo provider steering.",
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


def test_complete_retries_invalid_structured_output() -> None:
    provider = RecordingProvider(
        Message.assistant("not json"),
        Message.assistant('{"verdict":"pass","risks":[]}'),
    )

    result = complete(provider=provider, prompt="Summarize.", output_type=Summary)

    assert result.structured == Summary(verdict="pass", risks=[])
    assert len(provider.calls) == 2


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


def test_public_agent_saves_loads_and_restores_durable_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "agent-state.json"
    first = Agent(
        provider=RecordingProvider(Message.assistant("first")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    first.prompt("first")
    saved = first.save(state_path, metadata={"purpose": "test"})

    snapshot = AgentStateSnapshot.model_validate_json(saved.read_text(encoding="utf-8"))
    assert snapshot.format == "yoke.agent_state"
    assert snapshot.metadata == {"purpose": "test"}
    assert [message.content for message in snapshot.state.messages] == [
        "first",
        "first",
    ]

    resumed_provider = RecordingProvider(Message.assistant("second"))
    resumed = Agent.load(
        state_path,
        provider=resumed_provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    resumed.prompt("second")
    assert [message.content for message in resumed_provider.calls[-1][0][-3:]] == [
        "first",
        "first",
        "second",
    ]

    replacement = Agent(
        provider=RecordingProvider(Message.assistant("other")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    replacement.prompt("other")
    replacement.restore(state_path)
    assert [message.content for message in replacement.messages] == [
        "first",
        "first",
    ]
    assert replacement.state_path == state_path.resolve()


def test_public_agent_autosaves_only_successful_prompts(tmp_path: Path) -> None:
    state_path = tmp_path / "autosave.json"
    agent = Agent(
        provider=RecordingProvider(Message.assistant("done")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=state_path,
        autosave=True,
    )
    agent.prompt("hello")
    assert (
        AgentStateSnapshot.model_validate_json(state_path.read_text(encoding="utf-8"))
        .state.messages[-1]
        .content
        == "done"
    )

    failed_path = tmp_path / "failed.json"
    failing = Agent(
        provider=RecordingProvider(
            Message.assistant("bad"),
            Message.assistant("bad"),
            Message.assistant("bad"),
        ),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=failed_path,
        autosave=True,
    )
    with pytest.raises(StructuredOutputError):
        failing.prompt("hello", output_type=Summary)
    assert not failed_path.exists()


def test_public_agent_constructor_loads_existing_state_path(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    first = Agent(
        provider=RecordingProvider(Message.assistant("saved")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    first.prompt("saved")
    first.save(state_path)

    loaded = Agent(
        provider=RecordingProvider(Message.assistant("loaded")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=state_path,
    )
    assert loaded.has_state
    assert loaded.state_path == state_path.resolve()


def test_public_agent_validates_durable_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="autosave=True requires state_path"):
        Agent(
            provider=RecordingProvider(Message.assistant("done")),
            config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
            autosave=True,
        )
    agent = Agent(
        provider=RecordingProvider(Message.assistant("done")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    with pytest.raises(ValueError, match="requires a path"):
        agent.save()


def test_run_config_accepts_string_capability_ids(tmp_path: Path) -> None:
    provider = RecordingProvider(Message.assistant("done"))
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[
                "file.read",
                "file.search",
                "file.write",
                "file.extract_context",
                "web.fetch",
                "shell",
            ],
            include_agents_file=False,
        ),
    )
    agent.prompt("inspect")

    definitions = provider.calls[-1][1]
    names = {
        str(function["name"])
        for tool in definitions
        if (function := tool_function_payload(tool)) is not None
    }
    assert {"read", "extract_file_context", "web_fetch", "exec_command"} <= names
    assert {"edit", "write"} <= names
    assert "rg" in names or {"grep", "find", "ls"} <= names


def test_run_config_accepts_axi_history_fields(tmp_path: Path) -> None:
    provider = RecordingProvider(Message.assistant("done"))
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
            messages=[Message.user("previous")],
        ),
    )
    agent.prompt("next")
    assert [message.content for message in provider.calls[-1][0][-2:]] == [
        "previous",
        "next",
    ]

    with pytest.raises(ValueError, match="Provide only one"):
        RunConfig(
            root=tmp_path,
            messages=[Message.user("one")],
            conversation_entries=[],
        )


def test_public_agent_refreshes_provider_model_system_messages(
    tmp_path: Path,
) -> None:
    provider = ModelPromptProvider(
        Message.assistant("first"), Message.assistant("second")
    )
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            sys_prompt="Base instructions.",
            tools=[],
            include_agents_file=False,
        ),
    )

    agent.prompt("first")
    provider.config.model = "kimi-demo"
    agent.prompt("second")

    messages, _tools = provider.calls[-1]
    contents = [message.content for message in messages if message.role == "system"]
    assert "Base instructions." in contents
    assert "Use Kimi Demo provider steering." in contents
    assert "Use GPT Demo provider steering." not in contents


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


def test_public_agent_retries_invalid_structured_output(tmp_path: Path) -> None:
    provider = RecordingProvider(
        Message.assistant("not json"),
        Message.assistant('{"verdict":"pass","risks":[]}'),
    )
    agent = Agent(
        provider=provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    result = agent.prompt("Summarize.", output_type=Summary)

    assert result.structured == Summary(verdict="pass", risks=[])
    assert len(provider.calls) == 2
    retry_system = [
        message for message in provider.calls[-1][0] if message.role == "system"
    ]
    assert "adhere exactly to the schema" in (retry_system[-1].text_content() or "")


def test_public_agent_prompt_executes_local_tools(tmp_path: Path) -> None:
    provider = RecordingProvider(
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolFunction(
                        name="write",
                        arguments='{"path":"hello.txt","content":"hello"}',
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
            tools=[WriteTool],
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
    assert agent._runtime.tools["inspect_context"].context.model_key == ("demo:model-b")


def test_sdk_capabilities_register_contextual_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yoke.agent.capabilities.core.shutil.which",
        lambda name: "/usr/bin/rg" if name == "rg" else None,
    )
    provider = RecordingProvider(Message.assistant("done"))

    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            capabilities=[FileSearchCapability],
            include_agents_file=False,
        ),
    )

    assert list(agent._runtime.tools) == ["rg"]


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
