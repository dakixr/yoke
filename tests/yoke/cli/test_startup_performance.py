from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

from pathlib import Path
import json
import subprocess
import sys
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


def test_render_import_does_not_load_markdown_parser() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import yoke.cli.render; "
                "assert 'rich.markdown' not in sys.modules; "
                "assert 'markdown_it' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_mcp_tools_exist_without_importing_transport_stack(tmp_path: Path) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "sample": {
                        "command": "unused",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    code = f"""
import sys
from pathlib import Path
from yoke.agent.capabilities.builtins import McpCapability
from yoke.agent.tools import ModelIdentity, ToolRegistrationContext
from yoke.cli.bootstrap.config import ToolDiscoveryProvider

root = Path({str(tmp_path)!r})
provider = ToolDiscoveryProvider()
context = ToolRegistrationContext(
    root=root,
    home=root,
    provider=provider,
    model=ModelIdentity(provider_name=provider.provider_name),
)
tools = tuple(McpCapability().build_tools(context))
assert [tool.name for tool in tools] == ["mcp_inspect", "mcp_call"]
for module in ("yoke.mcp.manager", "yoke.mcp.client", "yoke.mcp.http_client"):
    assert module not in sys.modules, module
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_lazy_mcp_manager_constructs_real_manager_on_first_use(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from yoke.agent.tools.mcp import LazyMcpManager
    import yoke.mcp.manager as manager_module

    class FakeManager:
        closed = False

        def inspect(self, **_kwargs) -> dict[str, object]:
            return {"ok": True, "servers": []}

        def call_tool(self, **_kwargs) -> dict[str, object]:
            return {"ok": True, "text": "called"}

        def close(self) -> None:
            self.closed = True

    fake = FakeManager()
    created: list[dict[str, object]] = []

    class Factory:
        @staticmethod
        def from_paths(**kwargs):
            created.append(kwargs)
            return fake

    monkeypatch.setattr(manager_module, "McpManager", Factory)
    manager = LazyMcpManager(root=tmp_path, home=tmp_path)

    assert created == []
    assert manager.inspect() == {"ok": True, "servers": []}
    assert len(created) == 1
    assert manager.call_tool(
        server="sample",
        tool="demo",
        arguments={},
        cancel_requested=lambda: False,
    ) == {"ok": True, "text": "called"}
    assert len(created) == 1

    manager.close()
    assert fake.closed is True
