from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

from pathlib import Path
from types import SimpleNamespace

from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.cli.bootstrap.types import ResolvedAgentConfig
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config import runtime as runtime_module


class FakeProvider:
    provider_name = "fake"
    supports_image_inputs = False
    max_images_per_message = None


class FactoryCallingAgent:
    def __init__(
        self,
        *,
        provider,
        tool_factory,
        tool_root,
        tool_home,
        active_skills,
        **_kwargs,
    ) -> None:
        self.active_skills = list(active_skills)
        self.tool_report = None
        self._provider = provider
        self._tool_factory = tool_factory
        self._tool_root = tool_root
        self._tool_home = tool_home
        self.refresh_tools()

    def refresh_tools(self) -> None:
        self._tool_factory(
            ToolRegistrationContext(
                root=self._tool_root,
                home=self._tool_home,
                provider=self._provider,
                model=ModelIdentity(provider_name=self._provider.provider_name),
            )
        )


def test_agent_startup_reuses_initial_tool_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resolutions = []
    report = ToolLoadReport(discovered_tools=[], active_tools=[], denied_tools=[])

    def resolve(**kwargs):
        resolutions.append(kwargs)
        return ResolvedAgentConfig(
            system_messages=[],
            tools=[],
            tool_report=report,
            tool_system_messages=[],
        )

    monkeypatch.setattr(runtime_module, "prepare_provider_args", lambda _args: None)
    monkeypatch.setattr(runtime_module, "_load_cli_skill_registry", lambda _root: None)
    monkeypatch.setattr(
        runtime_module, "build_provider_from_args", lambda _args: FakeProvider()
    )
    monkeypatch.setattr(runtime_module, "_resolve_cli_agent_config", resolve)
    monkeypatch.setattr(
        runtime_module,
        "build_provider_context_manager",
        lambda **_kwargs: SimpleNamespace(instructions=[]),
    )
    monkeypatch.setattr(runtime_module, "RuntimeAgent", FactoryCallingAgent)

    built = runtime_module.build_cli_agent_from_args(
        runtime_module.CLIArgs(root=str(tmp_path))
    )

    assert len(resolutions) == 1
    assert built.tool_report is report
    assert built.agent.tool_report is report

    built.agent.refresh_tools()

    assert len(resolutions) == 2
