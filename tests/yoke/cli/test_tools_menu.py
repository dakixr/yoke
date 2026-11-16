"""Tests for the interactive tool policy menu."""

from __future__ import annotations


import io
import json
from typing import ClassVar

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.cli.bootstrap.types import LoadedTool, ToolLoadReport
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
    name = "command_exec"
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


def tool_report_with_active_names(
    report: ToolLoadReport, active_names: set[str]
) -> ToolLoadReport:
    """Frozen legacy menu algorithm, used only as the SDK projection reference."""
    return ToolLoadReport(
        discovered_tools=list(report.discovered_tools),
        active_tools=[
            entry
            for entry in report.discovered_tools
            if entry.tool.name in active_names
        ],
        denied_tools=[
            entry
            for entry in report.discovered_tools
            if entry.tool.name not in active_names
        ],
        config_path=report.config_path,
        unmatched_tool_names=report.unmatched_tool_names,
        unmatched_capability_ids=report.unmatched_capability_ids,
    )


def test_global_tool_change_preserves_hidden_runtime_tools(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"capabilities":{"shell":"deny"},'
        '"tools":{"command_exec":"deny","unrelated":"allow"}}',
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

    assert active_names == {"command_exec", "skill"}
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["capabilities"] == {"shell": "allow"}
    assert persisted["tools"] == {"unrelated": "allow"}
    assert notices == ["Updated tools for globally: enabled command_exec"]


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

    assert active_names == {"command_exec", "skill"}
    assert notices == ["No tool changes applied."]


def test_home_root_does_not_duplicate_global_scope(monkeypatch) -> None:
    captured_scopes: list[list[ToolChangeScope]] = []

    def capture_scopes(scopes, **_kwargs):
        captured_scopes.append(scopes)
        return None

    monkeypatch.setattr(tools_menu, "select_list_item_interactive", capture_scopes)

    assert tools_menu._select_tool_change_scope(root=tools_menu.Path.home()) is None
    assert [scope.id for scope in captured_scopes[0]] == ["session", "global"]


@pytest.mark.parametrize("factory_backed", [False, True])
@pytest.mark.parametrize(
    "enabled", [set(), {"command_exec"}, {"command_exec", "skill"}]
)
def test_public_tool_selection_preserves_legacy_provider_messages(
    tmp_path, factory_backed, enabled
):
    """Compare the public method to the exact pre-refactor menu algorithm."""
    from yoke.agent.context import ContextManager
    from yoke.agent.loop.tool_core import index_tools
    from yoke.agent.tools.context import ToolRegistrationResult
    from yoke.cli.bootstrap.types import LoadedSystemMessage

    class RecordingProvider(ProviderStub):
        def __init__(self):
            self.requests = []

        def complete(self, messages, tools):
            self.requests.append(
                ([message.model_dump_json() for message in messages], tools)
            )
            return Message.assistant("done")

    def factory(_context):
        return ToolRegistrationResult(
            tools=[VisibleTool.bind(), HiddenSkillTool.bind()],
            system_messages=[Message.system("registered tools\n  exact whitespace")],
        )

    agents = []
    providers = []
    for _ in range(2):
        provider = RecordingProvider()
        providers.append(provider)
        agent = RuntimeAgent(
            provider,
            [VisibleTool.bind(), HiddenSkillTool.bind()],
            tool_factory=factory if factory_backed else None,
            tool_root=tmp_path,
            context_manager=ContextManager(
                instructions=[Message.system("base instructions\n")]
            ),
        )
        report = _report(visible_active=True)
        report.system_messages = [
            LoadedSystemMessage(
                Message.system("shell instructions\n  preserve spacing"),
                "default",
                "shell",
                registration_id="capability:shell",
            ),
            LoadedSystemMessage(
                Message.system("unselected registration"),
                "default",
                "other",
                registration_id="other",
            ),
        ]
        agent.tool_report = report
        agents.append(agent)
    legacy, public = agents
    try:
        legacy_selection = [
            tool for name, tool in legacy.tools.items() if name in enabled
        ]
        public_selection = [
            tool for name, tool in public.tools.items() if name in enabled
        ]
        # The previous menu implementation, including report replacement order.
        if legacy._tool_factory is None:
            legacy.tools = index_tools(legacy_selection)
        else:
            legacy.set_session_enabled_tools(enabled)
        assert legacy.tool_report is not None
        legacy.tool_report = tool_report_with_active_names(legacy.tool_report, enabled)
        legacy._install_session_filtered_tool_system_messages()
        assert public.tool_report is not None
        public.apply_tool_selection(
            public_selection,
            report=tool_report_with_active_names(public.tool_report, enabled),
        )
        assert [
            message.model_dump_json() for message in public.context_manager.instructions
        ] == [
            message.model_dump_json() for message in legacy.context_manager.instructions
        ]
        legacy.run("test projection")
        public.run("test projection")
        assert providers[1].requests == providers[0].requests
    finally:
        for agent in agents:
            agent.close()
