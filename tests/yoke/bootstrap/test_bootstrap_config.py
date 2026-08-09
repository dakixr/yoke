# ruff: noqa

from __future__ import annotations

from .support import *  # noqa: F403


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
