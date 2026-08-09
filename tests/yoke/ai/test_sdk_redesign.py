# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from yoke.agent.models import Message
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.persistence import AgentStateSnapshot
from yoke.agent.tools import WorkspaceTool
from yoke.ai import Agent
from yoke.ai import AgentState
from yoke.ai import Image
from yoke.ai import RunConfig
from yoke.ai.skills import Skill
from yoke.ai.types import StructuredOutputError
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


class FastWriteTool(WorkspaceTool):
    name = "fast_write"
    description = "Write a text file inside the test workspace."
    execute_in_process = True
    marker: ClassVar[object] = lambda: None
    path: str
    text: str

    def execute(self) -> dict[str, object]:
        target = self._resolve_path(self.path, allow_missing=True)
        target.write_text(self.text, encoding="utf-8")
        return {"ok": True, "path": self.path}


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


def test_complete_retries_structured_output_failures() -> None:
    provider = RecordingProvider(
        Message.assistant("not json"),
        Message.assistant('{"verdict":"pass","risks":[]}'),
    )

    result = complete(
        provider=provider,
        prompt="Summarize.",
        output_type=Summary,
    )

    assert result.structured == Summary(verdict="pass", risks=[])
    assert len(provider.calls) == 2
    retry_messages, _tools = provider.calls[-1]
    retry_system_messages = [
        message for message in retry_messages if message.role == "system"
    ]
    assert retry_system_messages
    assert "adhere exactly to the schema" in (
        retry_system_messages[-1].text_content() or ""
    )


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


def test_public_agent_transcripts_are_snapshots(tmp_path: Path) -> None:
    agent = Agent(
        provider=RecordingProvider(Message.assistant("done")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    result = agent.prompt("hello")
    result.messages.clear()
    assert result.conversation_entries is not None
    result.conversation_entries.clear()
    messages = agent.messages
    entries = agent.conversation_entries
    messages.clear()
    entries.clear()

    assert [message.content for message in agent.messages] == ["hello", "done"]
    assert len(agent.conversation_entries) == 2
    agent.close()


def test_public_agent_prompt_executes_local_tools(tmp_path: Path) -> None:
    provider = RecordingProvider(
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolFunction(
                        name="fast_write",
                        arguments='{"path":"hello.txt","text":"hello"}',
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
            tools=[FastWriteTool],
            include_agents_file=False,
        ),
    )

    result = agent.prompt("Create a file.")

    assert result.output == "done"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"


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


def test_public_agent_saves_and_loads_durable_state(tmp_path: Path) -> None:
    state_path = tmp_path / "agent-state.json"
    first_provider = RecordingProvider(Message.assistant("first"))
    agent = Agent(
        provider=first_provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    agent.prompt("first")
    saved_path = agent.save(state_path, metadata={"purpose": "test"})

    snapshot = AgentStateSnapshot.model_validate_json(
        saved_path.read_text(encoding="utf-8")
    )
    assert snapshot.format == "yoke.agent_state"
    assert snapshot.metadata == {"purpose": "test"}
    assert len(snapshot.state.messages) == 2

    second_provider = RecordingProvider(Message.assistant("second"))
    resumed = Agent.load(
        state_path,
        provider=second_provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    resumed.prompt("second")

    messages, _tools = second_provider.calls[-1]
    assert [message.content for message in messages[-3:]] == [
        "first",
        "first",
        "second",
    ]


def test_public_agent_constructor_loads_existing_state_path(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "agent-state.json"
    provider = RecordingProvider(Message.assistant("saved"))
    agent = Agent(
        provider=provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    agent.prompt("saved")
    agent.save(state_path)

    loaded_provider = RecordingProvider(Message.assistant("loaded"))
    loaded = Agent(
        provider=loaded_provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=state_path,
    )

    assert loaded.state_path == state_path.resolve()
    assert loaded.has_state
    assert [message.content for message in loaded.messages] == [
        "saved",
        "saved",
    ]


def test_public_agent_restore_replaces_state(tmp_path: Path) -> None:
    first_path = tmp_path / "first.json"
    first = Agent(
        provider=RecordingProvider(Message.assistant("first")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    first.prompt("first")
    first.save(first_path)

    agent = Agent(
        provider=RecordingProvider(Message.assistant("second")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )
    agent.prompt("second")

    agent.restore(first_path)

    assert [message.content for message in agent.messages] == [
        "first",
        "first",
    ]
    assert agent.state_path == first_path.resolve()


def test_public_agent_autosaves_after_successful_prompt(tmp_path: Path) -> None:
    state_path = tmp_path / "autosave.json"
    agent = Agent(
        provider=RecordingProvider(Message.assistant("done")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=state_path,
        autosave=True,
    )

    agent.prompt("hello")

    snapshot = AgentStateSnapshot.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    assert [message.content for message in snapshot.state.messages] == [
        "hello",
        "done",
    ]


def test_public_agent_does_not_autosave_after_failed_prompt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "autosave.json"
    agent = Agent(
        provider=RecordingProvider(Message.assistant("not json")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=state_path,
        autosave=True,
    )

    with pytest.raises(StructuredOutputError):
        agent.prompt("hello", output_type=Summary)

    assert not state_path.exists()


def test_public_agent_retries_structured_output_failures(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(
        Message.assistant("not json"),
        Message.assistant('{"verdict":"pass","risks":[]}'),
    )
    agent = Agent(
        provider=provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    result = agent.prompt("hello", output_type=Summary)

    assert result.structured == Summary(verdict="pass", risks=[])
    assert len(provider.calls) == 2
    retry_messages, _tools = provider.calls[-1]
    retry_system_messages = [
        message for message in retry_messages if message.role == "system"
    ]
    assert retry_system_messages
    assert "adhere exactly to the schema" in (
        retry_system_messages[-1].text_content() or ""
    )


def test_public_agent_autosave_requires_state_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="autosave=True requires state_path"):
        Agent(
            provider=RecordingProvider(Message.assistant("done")),
            config=RunConfig(
                root=tmp_path,
                tools=[],
                include_agents_file=False,
            ),
            autosave=True,
        )


def test_public_agent_save_requires_path_when_unbound(tmp_path: Path) -> None:
    agent = Agent(
        provider=RecordingProvider(Message.assistant("done")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    with pytest.raises(ValueError, match="requires a path"):
        agent.save()


def test_public_agent_resolves_implicit_save_target_after_lock_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_path = tmp_path / "old.json"
    current_path = tmp_path / "current.json"
    agent = Agent(
        provider=RecordingProvider(Message.assistant("done")),
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
        state_path=old_path,
    )
    agent.prompt("hello")
    ensure_open = agent._ensure_open

    def rebind_after_lock_entry() -> None:
        ensure_open()
        agent._state_path = current_path

    monkeypatch.setattr(agent, "_ensure_open", rebind_after_lock_entry)

    assert agent.save() == current_path
    assert current_path.exists()
    assert not old_path.exists()
    agent.close()


def test_public_agent_state_is_reexported() -> None:
    assert AgentState.__name__ == "AgentState"
