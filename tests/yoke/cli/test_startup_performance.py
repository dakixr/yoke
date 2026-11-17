from __future__ import annotations

# ruff: noqa: ANN001,D100,D103,S101

from pathlib import Path

from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext


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
