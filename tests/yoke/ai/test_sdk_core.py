# ruff: noqa: F403, F405
# ruff: noqa

from __future__ import annotations

from .support import *  # noqa: F403, F405


def test_ai_complete_accepts_context() -> None:
    provider = StaticProvider(Message.assistant("done"))

    result = complete(
        provider=provider,
        context=Context.from_prompt("hello", sys_prompt="sys"),
    )

    assert result.output == "done"
    assert [message.role for message in result.messages] == [
        "system",
        "user",
        "assistant",
    ]


def test_sdk_agent_prompt_executes_workspace_tool_classes(
    tmp_path: Path,
) -> None:
    first_response = StaticProvider(
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
        )
    )
    final_response = StaticProvider(Message.assistant("done"))

    class TwoStepProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            self.calls += 1
            return first_response.message if self.calls == 1 else final_response.message

    agent = Agent(
        provider=TwoStepProvider(),
        config=RunConfig(
            root=tmp_path,
            tools=[WriteTool],
            include_agents_file=False,
        ),
    )
    result = agent.prompt("create file")

    assert result.output == "done"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"


def test_sdk_agent_prompt_is_stateful(tmp_path: Path) -> None:
    class HistoryProvider(Provider):
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls.append([message.model_copy(deep=True) for message in messages])
            return Message.assistant("done")

    provider = HistoryProvider()
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
        ),
    )

    agent.prompt("first")
    agent.prompt("second")

    assert [message.role for message in provider.calls[1][-3:]] == [
        "user",
        "assistant",
        "user",
    ]
    assert provider.calls[1][-1].content == "second"


def test_sdk_agent_accepts_tagged_message_history(tmp_path: Path) -> None:
    agent = Agent(
        provider=StaticProvider(Message.assistant("done")),
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
            history=MessageHistory([Message.user("previous")]),
        ),
    )

    result = agent.prompt("next")

    assert [message.content for message in result.messages[:2]] == [
        "previous",
        "next",
    ]


def test_sdk_agent_does_not_load_repo_tools_by_default(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".yoke" / "plugins"
    tools_dir.mkdir(parents=True)
    (tools_dir / "repo_echo.py").write_text(
        """
from yoke.cli.tools.decorators import function_tool


@function_tool(name="repo_echo")
def repo_echo(text: str) -> dict[str, object]:
    return {"ok": True, "text": text}
""".strip(),
        encoding="utf-8",
    )

    class NoRepoToolProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            tool_names: set[str] = set()
            for tool in tools:
                fn = tool_function_payload(tool)
                if fn is None:
                    continue
                name = fn.get("name")
                if isinstance(name, str):
                    tool_names.add(name)
            assert "repo_echo" not in tool_names
            return Message.assistant("done")

    agent = Agent(
        provider=NoRepoToolProvider(),
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
        ),
    )

    result = agent.prompt("inspect tools")

    assert result.output == "done"


def test_sdk_agent_renders_file_backed_skills(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        ("---\nname: demo-skill\ndescription: Demo skill.\n---\n\nAct carefully.\n"),
        encoding="utf-8",
    )
    seen_messages: list[Message] = []

    class InspectProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            seen_messages[:] = messages
            return Message.assistant("done")

    agent = Agent(
        provider=InspectProvider(),
        config=RunConfig(
            root=tmp_path,
            tools=[],
            include_agents_file=False,
            skills=[Skill.from_dir(skill_dir)],
        ),
    )
    result = agent.prompt("hello")

    assert result.output == "done"
    combined = "\n".join(message.text_content() or "" for message in seen_messages)
    assert "Active skill:" in combined
    assert "demo-skill" in combined


def test_sdk_agent_accepts_explicit_extra_tools(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("a\nb\nc\n", encoding="utf-8")

    class ExtraToolProvider(Provider):
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
                assert "extract_file_context" in tool_names
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            function=ToolFunction(
                                name="extract_file_context",
                                arguments='{"path":"notes.txt"}',
                            ),
                        )
                    ],
                )
            assert "extractor" in (messages[-1].content or "")
            return Message.assistant("done")

    agent = Agent(
        provider=ExtraToolProvider(),
        config=RunConfig(
            root=tmp_path,
            tools=[ExtractFileContextTool],
            include_agents_file=False,
        ),
    )
    result = agent.prompt("count lines")

    assert result.output == "done"
