"""Menu ordering is part of the provider request contract."""

import io

from yoke.agent.loop.agent import RuntimeAgent
from yoke.cli.bootstrap.types import LoadedTool, ToolLoadReport
from yoke.cli.interactive import tools_menu
from yoke.cli.interactive.tools_menu import ToolChangeScope
from yoke.cli.render import build_console

from .test_tools_menu import HiddenSkillTool, VisibleTool
from .test_turn_projection import RecordingProvider


class AlphaTool(VisibleTool):
    name = "alpha"


class ZetaTool(VisibleTool):
    name = "zeta"


def test_menu_backend_preserves_provider_tool_order_and_hidden_tools(monkeypatch):
    # Discovery order differs from menu row order. Keeping discovery order here
    # would silently change the provider request and its cache prefix.
    discovered = [
        LoadedTool(ZetaTool.bind(), "default", "zeta"),
        LoadedTool(AlphaTool.bind(), "default", "alpha"),
    ]
    provider, reference_provider = RecordingProvider(), RecordingProvider()
    runtime = RuntimeAgent(provider, [ZetaTool.bind(), HiddenSkillTool.bind()])
    reference = RuntimeAgent(
        reference_provider, [AlphaTool.bind(), ZetaTool.bind(), HiddenSkillTool.bind()]
    )
    runtime.tool_report = ToolLoadReport(discovered, [discovered[0]], [discovered[1]])
    monkeypatch.setattr(
        tools_menu, "select_table_items_interactive", lambda *_a, **_kw: {0, 1}
    )
    monkeypatch.setattr(
        tools_menu,
        "_select_tool_change_scope",
        lambda **_kw: ToolChangeScope("session", "This session", "temporary"),
    )
    try:
        tools_menu.handle_tools_menu(
            agent=runtime, console=build_console(io.StringIO())
        )
        assert list(runtime.tools) == ["alpha", "zeta", "skill"]
        runtime.run("Compare the selected tools.")
        reference.run("Compare the selected tools.")
        assert provider.requests == reference_provider.requests
    finally:
        runtime.close()
        reference.close()
