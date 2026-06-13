from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002, ANN003, ANN401, D100, D103, F403, F405, S101

from collections.abc import Callable, Sequence
import json
from typing import Any

from yoke import __version__
from yoke.cli.runtime import execute_turn

from .support import *  # noqa: F403, F405


def test_interactive_cli_intro_prints_version(tmp_path: Path) -> None:
    prompts = iter(["quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = EncodedTTYCaptureStream()
    stderr = CaptureStream()
    exit_code = run_cli(
        CLIArgs(root=str(tmp_path)),
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert f"Version {__version__}" in output


def test_interactive_cli_supports_slash_commands(tmp_path: Path) -> None:
    class SlashAgent:
        supports_message_history = True
        supports_user_message = False

        def run(
            self,
            prompt: str,
            messages: Sequence[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Callable[[], bool] | None = None,
        ) -> AgentResult:
            del on_event, stop_requested
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            conversation.append(Message.assistant("done"))
            return AgentResult(output="done", messages=conversation, iterations=1)

    prompts = iter(["/shortcuts", "?", "/compact", "/new", "hello", "quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_cli(
        CLIArgs(root=str(tmp_path)),
        agent=SlashAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert output.count(SHORTCUTS_NOTICE) == 2
    assert COMPACTION_IN_PROGRESS_NOTICE in output
    assert "Nothing to compact right now." in output
    assert "Started new session" in output
    assert "done" in output


def test_new_slash_command_resets_runtime_agent_context(
    tmp_path: Path,
) -> None:
    class RecordingProvider(Provider):
        seen_actual_contexts: list[list[str]]

        def __init__(self) -> None:
            self.seen_actual_contexts = []

        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            del tools
            if "Create a concise title" in (messages[0].content or ""):
                return Message.assistant("test title")
            self.seen_actual_contexts.append([message.role for message in messages])
            return Message.assistant("done")

    provider = RecordingProvider()
    agent = RuntimeAgent(provider=provider, tools=[])
    old_session = active_session_for(tmp_path)
    agent.run("first")

    from yoke.cli.interactive.slash_commands import handle_slash_command

    handled, messages, new_session = handle_slash_command(
        "/new",
        agent=agent,
        active_session=old_session,
        messages=agent.messages,
        console=build_console(CaptureStream()),
    )
    agent.run("after")

    assert handled
    assert messages == []
    assert new_session.id != old_session.id
    assert provider.seen_actual_contexts == [
        ["user"],
        ["user"],
    ]


def test_runtime_agent_accepts_empty_conversation_entries() -> None:
    class RecordingProvider(Provider):
        seen_actual_contexts: list[list[str]]

        def __init__(self) -> None:
            self.seen_actual_contexts = []

        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            del tools
            self.seen_actual_contexts.append([message.role for message in messages])
            return Message.assistant("done")

    provider = RecordingProvider()
    agent = RuntimeAgent(provider=provider, tools=[])

    result = execute_turn(
        agent,
        "after new",
        [Message.user("old message")],
        conversation_entries=[],
        indicator=None,
    )

    assert result.output == "done"
    assert provider.seen_actual_contexts == [["user"]]


def test_title_slash_command_renames_active_session(tmp_path: Path) -> None:
    from yoke.cli.interactive.slash_commands import handle_slash_command

    active_session = active_session_for(tmp_path)
    agent = FakeAgent()
    stdout = CaptureStream()

    handled, messages, updated_session = handle_slash_command(
        "/title   Demo   Session  ",
        agent=agent,
        active_session=active_session,
        messages=[],
        console=build_console(stdout),
    )

    assert handled is True
    assert messages == []
    assert updated_session.title == "Demo Session"
    assert SessionStore().load(active_session.id).title == "Demo Session"
    assert "Updated session title: Demo Session" in stdout.getvalue()


def test_title_slash_command_requires_title(tmp_path: Path) -> None:
    from yoke.cli.interactive.slash_commands import handle_slash_command

    active_session = active_session_for(tmp_path)
    stdout = CaptureStream()

    handled, messages, updated_session = handle_slash_command(
        "/title",
        agent=FakeAgent(),
        active_session=active_session,
        messages=[],
        console=build_console(stdout),
    )

    assert handled is True
    assert messages == []
    assert updated_session.title is None
    assert SessionStore().load(active_session.id).title is None
    assert "Usage: /title <new-title>" in stdout.getvalue()


def test_tools_menu_applies_session_only_runtime_tool_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from yoke.agent.tools import ReadTool
    from yoke.agent.tools import WebFetchTool
    from yoke.cli.bootstrap.types import LoadedTool
    from yoke.cli.bootstrap.types import ToolLoadReport
    from yoke.cli.interactive import tools_menu
    from yoke.cli.interactive.slash_commands import handle_slash_command

    read_tool = ReadTool.bind(root=tmp_path)
    web_tool = WebFetchTool.bind()
    agent = RuntimeAgent(provider=TitleProvider("done"), tools=[read_tool])
    agent.tool_report = ToolLoadReport(
        discovered_tools=[
            LoadedTool(
                tool=read_tool,
                source_kind="default",
                source_label="builtin",
            ),
            LoadedTool(
                tool=web_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
        active_tools=[
            LoadedTool(
                tool=read_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
        denied_tools=[
            LoadedTool(
                tool=web_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
    )

    monkeypatch.setattr(
        tools_menu,
        "select_table_items_interactive",
        lambda *args, **kwargs: {1},
    )
    monkeypatch.setattr(
        tools_menu,
        "select_list_item_interactive",
        lambda items, *args, **kwargs: items[0],
    )
    stdout = CaptureStream()
    console = build_console(stdout)

    handled, messages, _session = handle_slash_command(
        "/tools",
        agent=agent,
        active_session=active_session_for(tmp_path),
        messages=[],
        console=console,
    )

    assert handled is True
    assert messages == []
    assert set(agent.tools) == {"web_fetch"}
    assert "Updated tools for this session" in stdout.getvalue()
    config_path = tmp_path / ".yoke" / "config.json"
    assert not config_path.exists()


def test_tools_menu_can_persist_runtime_tool_overrides_to_root_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from yoke.agent.tools import ReadTool
    from yoke.agent.tools import WebFetchTool
    from yoke.cli.bootstrap.types import LoadedTool
    from yoke.cli.bootstrap.types import ToolLoadReport
    from yoke.cli.interactive import tools_menu
    from yoke.cli.interactive.slash_commands import handle_slash_command

    read_tool = ReadTool.bind(root=tmp_path)
    web_tool = WebFetchTool.bind()
    agent = RuntimeAgent(provider=TitleProvider("done"), tools=[read_tool])
    agent.tool_report = ToolLoadReport(
        discovered_tools=[
            LoadedTool(
                tool=read_tool,
                source_kind="default",
                source_label="builtin",
            ),
            LoadedTool(
                tool=web_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
        active_tools=[
            LoadedTool(
                tool=read_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
        denied_tools=[
            LoadedTool(
                tool=web_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
    )

    monkeypatch.setattr(
        tools_menu,
        "select_table_items_interactive",
        lambda *args, **kwargs: {1},
    )
    monkeypatch.setattr(
        tools_menu,
        "select_list_item_interactive",
        lambda items, *args, **kwargs: items[1],
    )
    stdout = CaptureStream()

    handled, _messages, _session = handle_slash_command(
        "/tools",
        agent=agent,
        active_session=active_session_for(tmp_path),
        messages=[],
        console=build_console(stdout),
    )

    assert handled is True
    assert set(agent.tools) == {"web_fetch"}
    config = json.loads((tmp_path / ".yoke" / "config.json").read_text())
    assert config["tools"] == {"read": "deny", "web_fetch": "allow"}
    assert "Updated tools for this root path" in stdout.getvalue()


def test_tools_menu_preserves_hidden_runtime_only_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from yoke.agent.skills.registry import load_skill_registry
    from yoke.agent.tools import ReadTool
    from yoke.agent.tools import SkillTool
    from yoke.agent.tools import WebFetchTool
    from yoke.cli.bootstrap.types import LoadedTool
    from yoke.cli.bootstrap.types import ToolLoadReport
    from yoke.cli.interactive import tools_menu

    skill_dir = tmp_path / ".yoke" / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review code.\n---\n\nBe strict.\n",
        encoding="utf-8",
    )
    registry = load_skill_registry([tmp_path / ".yoke" / "skills"])
    assert registry is not None
    read_tool = ReadTool.bind(root=tmp_path)
    web_tool = WebFetchTool.bind()
    skill_tool = SkillTool.bind(skill_registry=registry, active_skills=[])
    agent = RuntimeAgent(
        provider=TitleProvider("done"),
        tools=[read_tool, skill_tool],
        skill_registry=registry,
        available_skills=registry.skills,
    )
    agent.tool_report = ToolLoadReport(
        discovered_tools=[
            LoadedTool(
                tool=read_tool,
                source_kind="default",
                source_label="builtin",
            ),
            LoadedTool(
                tool=web_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
        active_tools=[
            LoadedTool(
                tool=read_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
        denied_tools=[
            LoadedTool(
                tool=web_tool,
                source_kind="default",
                source_label="builtin",
            ),
        ],
    )

    monkeypatch.setattr(
        tools_menu,
        "select_table_items_interactive",
        lambda *args, **kwargs: {1},
    )
    monkeypatch.setattr(
        tools_menu,
        "select_list_item_interactive",
        lambda items, *args, **kwargs: items[0],
    )

    tools_menu.handle_tools_menu(
        agent=agent,
        console=build_console(CaptureStream()),
    )

    assert set(agent.tools) == {"web_fetch", "skill"}
