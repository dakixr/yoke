"""Tests for the interactive tool policy menu."""

from __future__ import annotations

import io
import json
from typing import ClassVar

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.cli.bootstrap.types import LoadedTool
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.interactive import tools_menu
from yoke.cli.interactive.tools_menu import ToolChangeScope
from yoke.cli.render import build_console


class ProviderStub:
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")


class VisibleTool(LocalTool):
    name = "exec_command"
    description = "Visible menu tool."

    def execute(self) -> dict[str, object]:
        return {"ok": True}


class HiddenSkillTool(LocalTool):
    name = "skill"
    description = "Runtime-injected skill tool."

    def execute(self) -> dict[str, object]:
        return {"ok": True}


def _report(*, visible_active: bool) -> ToolLoadReport:
    loaded = LoadedTool(
        tool=VisibleTool.bind(),
        source_kind="default",
        source_label="default:builtin",
        capability_id="shell",
        registration_id="capability:shell",
    )
    return ToolLoadReport(
        discovered_tools=[loaded],
        active_tools=[loaded] if visible_active else [],
        denied_tools=[] if visible_active else [loaded],
    )


def test_global_tool_change_preserves_hidden_runtime_tools(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"capabilities":{"shell":"deny"},'
        '"tools":{"exec_command":"deny","unrelated":"allow"}}',
        encoding="utf-8",
    )
    notices: list[str] = []
    agent = RuntimeAgent(provider=ProviderStub(), tools=[HiddenSkillTool.bind()])
    agent.tool_report = _report(visible_active=False)
    monkeypatch.setattr(
        tools_menu,
        "select_table_items_interactive",
        lambda *_args, **_kwargs: {0},
    )
    monkeypatch.setattr(
        tools_menu,
        "_select_tool_change_scope",
        lambda **_kwargs: ToolChangeScope("global", "Globally", "test"),
    )
    monkeypatch.setattr(
        tools_menu,
        "_tool_scope_config_path",
        lambda **_kwargs: config_path,
    )
    monkeypatch.setattr(
        "yoke.cli.render.print_scrollback_notice",
        lambda _console, message: notices.append(message),
    )

    try:
        tools_menu.handle_tools_menu(
            agent=agent,
            console=build_console(io.StringIO()),
            root=tmp_path,
        )
        active_names = set(agent.tools)
    finally:
        agent.close()

    assert active_names == {"exec_command", "skill"}
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["capabilities"] == {"shell": "allow"}
    assert persisted["tools"] == {"unrelated": "allow"}
    assert notices == ["Updated tools for globally: enabled exec_command"]


def test_unchanged_visible_selection_ignores_hidden_runtime_tools(
    tmp_path, monkeypatch
) -> None:
    notices: list[str] = []
    agent = RuntimeAgent(
        provider=ProviderStub(),
        tools=[VisibleTool.bind(), HiddenSkillTool.bind()],
    )
    agent.tool_report = _report(visible_active=True)
    monkeypatch.setattr(
        tools_menu,
        "select_table_items_interactive",
        lambda *_args, **_kwargs: {0},
    )
    monkeypatch.setattr(
        tools_menu,
        "_select_tool_change_scope",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("An unchanged visible selection must not ask for scope.")
        ),
    )
    monkeypatch.setattr(
        "yoke.cli.render.print_scrollback_notice",
        lambda _console, message: notices.append(message),
    )

    try:
        tools_menu.handle_tools_menu(
            agent=agent,
            console=build_console(io.StringIO()),
            root=tmp_path,
        )
        active_names = set(agent.tools)
    finally:
        agent.close()

    assert active_names == {"exec_command", "skill"}
    assert notices == ["No tool changes applied."]


def test_home_root_does_not_duplicate_global_scope(monkeypatch) -> None:
    captured_scopes: list[list[ToolChangeScope]] = []

    def capture_scopes(scopes, **_kwargs):
        captured_scopes.append(scopes)
        return None

    monkeypatch.setattr(tools_menu, "select_list_item_interactive", capture_scopes)

    assert tools_menu._select_tool_change_scope(root=tools_menu.Path.home()) is None
    assert [scope.id for scope in captured_scopes[0]] == ["session", "global"]
