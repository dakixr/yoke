# ruff: noqa: F403, F405
# ruff: noqa

from __future__ import annotations

from .support import *  # noqa: F403, F405


def test_sdk_agent_includes_global_and_repo_agents_files_as_system_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".yoke").mkdir(parents=True)
    (home / ".yoke" / "AGENTS.md").write_text("Follow global rules.", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Follow repo rules.", encoding="utf-8")
    monkeypatch.setattr("yoke.cli.bootstrap.agents.Path.home", lambda: home)
    provider = StaticProvider(Message.assistant("done"))

    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[],
            sys_prompt="base prompt",
        ),
    )
    result = agent.prompt("hello")

    assert result.output == "done"
    assert [message.role for message in provider.calls[0][:4]] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert provider.calls[0][0].content == "base prompt"
    assert "Follow global rules." in (provider.calls[0][1].content or "")
    assert "Follow repo rules." in (provider.calls[0][2].content or "")


def test_resolve_agent_config_loads_repo_tools(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".yoke" / "tools"
    tools_dir.mkdir(parents=True)
    (tmp_path / ".yoke" / "config.json").write_text(
        '{"tools": {"repo_echo": "allow"}}\n', encoding="utf-8"
    )
    (tools_dir / "repo_echo.py").write_text(
        """
from pydantic import Field

from yoke.agent.tools import LocalTool


class RepoEchoTool(LocalTool):
    name = "repo_echo"
    description = "Echo text from a repo-local tool."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return {"ok": True, "text": self.text, "root": str(self._context["root"])}


def register_tools(context):
    return [RepoEchoTool.bind(root=context.root)]
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )
    result = execute_tool(resolved.tools, "repo_echo", {"text": "hello"})

    assert result["ok"] is True
    assert result["text"] == "hello"
    assert result["root"] == str(tmp_path.resolve())


def test_repo_tool_registration_can_contribute_system_messages(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir()
    (tools_dir / "config.json").write_text(
        '{"tools": {"prompt_tool": "allow"}}\n',
        encoding="utf-8",
    )
    (tools_dir / "prompt_tool.py").write_text(
        """
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool, ToolRegistrationResult


class PromptTool(LocalTool):
    name = "prompt_tool"
    description = "A tool with registration-time instructions."

    def execute(self) -> dict[str, object]:
        return {"ok": True}


def register_tools(context):
    return ToolRegistrationResult(
        tools=[PromptTool.bind()],
        system_messages=[Message.system("Use prompt_tool carefully.")],
    )
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    assert "Use prompt_tool carefully." in {
        message.content for message in resolved.tool_system_messages
    }


def test_denied_tool_does_not_contribute_system_messages(tmp_path: Path) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        '{"tools": {"apply_patch": "deny"}}\n',
        encoding="utf-8",
    )

    class GPTProvider:
        provider_name = "demo"
        config = type("Config", (), {"model": "gpt-coder"})()

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
        provider=GPTProvider(),
    )

    assert "apply_patch" not in {tool.name for tool in resolved.tools}
    assert resolved.tool_system_messages == []


def test_resolve_agent_config_loads_recursive_repo_tools_under_dot_pi(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".yoke" / "nested" / "utilities"
    tools_dir.mkdir(parents=True)
    (tmp_path / ".yoke" / "config.json").write_text(
        '{"tools": {"deep_echo": "allow"}}\n', encoding="utf-8"
    )
    (tools_dir / "deep_echo.py").write_text(
        """
from pydantic import Field

from yoke.agent.tools import LocalTool


class DeepEchoTool(LocalTool):
    name = "deep_echo"
    description = "Echo text from a deeply nested repo tool."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return {"ok": True, "text": self.text}


def register_tools(context):
    return [DeepEchoTool.bind()]
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )
    result = execute_tool(resolved.tools, "deep_echo", {"text": "hello"})

    assert result["ok"] is True
    assert result["text"] == "hello"


def test_resolve_agent_config_discovers_class_and_function_tools(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "config.json").write_text(
        '{"tools": {"shout": "allow", "count_chars": "allow"}}\n',
        encoding="utf-8",
    )
    (tools_dir / "decorator_tools.py").write_text(
        """
from pydantic import Field

from yoke.agent.tools import WorkspaceTool
from yoke.cli.tools.decorators import class_tool, function_tool


@function_tool
def shout(text: str) -> dict[str, object]:
    return {"ok": True, "text": text.upper()}


@class_tool
class CountCharsTool(WorkspaceTool):
    name = "count_chars"
    description = "Count chars in text."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return self._success(length=len(self.text))
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )
    shout = execute_tool(resolved.tools, "shout", {"text": "hello"})
    count = execute_tool(resolved.tools, "count_chars", {"text": "hello"})

    assert shout["ok"] is True
    assert shout["text"] == "HELLO"
    assert count["ok"] is True
    assert count["length"] == 5


def test_sdk_does_not_load_global_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    tools_dir = home / ".yoke" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "global_echo.py").write_text(
        """
from pydantic import Field

from yoke.agent.tools import LocalTool


class GlobalEchoTool(LocalTool):
    name = "global_echo"
    description = "Echo text from a global tool."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return {"ok": True, "text": self.text}


def register_tools(context):
    return [GlobalEchoTool.bind()]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    provider = StaticProvider(Message.assistant("done"))
    default_agent = Agent(
        provider=provider,
        config=RunConfig(root=tmp_path, tools=[]),
    )

    default_names = definition_names(default_agent)

    assert "global_echo" not in default_names


def test_cli_agent_loads_global_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    tools_dir = home / ".yoke" / "tools"
    tools_dir.mkdir(parents=True)
    (home / ".yoke" / "config.json").write_text(
        '{"tools": {"global_echo": "allow"}}\n', encoding="utf-8"
    )
    (tools_dir / "global_echo.py").write_text(
        """
from pydantic import Field

from yoke.agent.tools import LocalTool


class GlobalEchoTool(LocalTool):
    name = "global_echo"
    description = "Echo text from a global tool."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return {"ok": True, "text": self.text}


def register_tools(context):
    return [GlobalEchoTool.bind()]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    names = report_names(build_tool_report(root=tmp_path))

    assert "global_echo" in names


def test_repo_tools_override_builtin_tools(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".yoke" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "duplicate.py").write_text(
        """
from yoke.agent.tools import LocalTool


class DuplicateReadTool(LocalTool):
    name = "read"
    description = "Conflicts with the builtin read tool."

    def execute(self) -> dict[str, object]:
        return {"ok": True}


def register_tools(context):
    return [DuplicateReadTool.bind()]
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )
    result = execute_tool(resolved.tools, "read", {"path": "whatever.txt"})

    assert result["ok"] is True


def test_conflicting_same_precedence_tools_raise_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    global_tools_dir = home / ".yoke"
    global_tools_dir.mkdir(parents=True)
    (global_tools_dir / "one.py").write_text(
        """
from yoke.agent.tools import LocalTool


class OneTool(LocalTool):
    name = "conflict"
    description = "first"

    def execute(self) -> dict[str, object]:
        return {"ok": True, "source": "one"}


def register_tools(context):
    return [OneTool.bind()]
""".strip(),
        encoding="utf-8",
    )
    (global_tools_dir / "two.py").write_text(
        """
from yoke.agent.tools import LocalTool


class TwoTool(LocalTool):
    name = "conflict"
    description = "second"

    def execute(self) -> dict[str, object]:
        return {"ok": True, "source": "two"}


def register_tools(context):
    return [TwoTool.bind()]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    with pytest.raises(
        ValueError, match="Same-precedence tools cannot override each other"
    ):
        resolve_agent_config(
            root=tmp_path,
            base_system_prompt=None,
            include_global_tools=True,
        )


def test_workspace_config_can_deny_tools_with_wildcards(tmp_path: Path) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """
{
  "tools": {
    "e*": "deny",
    "read": "allow"
  }
}
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )
    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}
    denied_names = {entry.tool.name for entry in resolved.tool_report.denied_tools}

    assert "edit" in denied_names
    assert "read" not in denied_names
    assert "read" in active_names


def test_document_and_web_tools_are_builtin_tools(tmp_path: Path) -> None:
    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}

    assert "extract_file_context" in active_names
