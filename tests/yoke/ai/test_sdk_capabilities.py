# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yoke.agent.models import Message
from yoke.ai import CapabilityInfo
from yoke.ai import default_coding_agent_tools
from yoke.ai import discover_capabilities
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo


class CatalogProvider(Provider):
    provider_name = "catalog"
    supports_image_inputs = False

    def __init__(self, model: str = "gpt-catalog") -> None:
        self.config = SimpleNamespace(model=model, reasoning_effort="high")
        self.closed = False

    def complete(self, messages, tools):
        del messages, tools
        return Message.assistant("done")

    def current_model_info(self) -> ProviderModelInfo:
        return ProviderModelInfo(
            id=self.config.model,
            display_name="Catalog Model",
            context_window_tokens=100_000,
            thinking_levels=("high",),
            supports_image_inputs=False,
        )

    def close(self) -> None:
        self.closed = True


def test_default_coding_tools_remain_explicit_historical_capabilities() -> None:
    assert default_coding_agent_tools() == [
        "image.attach",
        "file.extract_context",
        "file.search",
        "file.read",
        "web.fetch",
        "web.research",
        "file.write",
        "shell",
    ]


def test_discover_capabilities_resolves_provider_aware_tools(
    tmp_path,
) -> None:
    capability_ids = [
        "image.attach",
        "file.extract_context",
        "file.search",
        "file.read",
        "web.fetch",
        "web.research",
        "file.write",
        "shell",
    ]
    discovered = discover_capabilities(
        CatalogProvider(),
        root=tmp_path,
        home=tmp_path,
        capability_ids=capability_ids,
    )
    by_id = {capability.id: capability for capability in discovered}

    assert all(isinstance(capability, CapabilityInfo) for capability in discovered)
    assert tuple(by_id) == tuple(capability_ids)
    assert by_id["file.read"].tool_names == ("read",)
    assert by_id["file.write"].tool_names == ("apply_patch",)
    assert by_id["file.write"].aliases == ("file.edit",)
    assert not by_id["image.attach"].available
    assert by_id["image.attach"].tool_names == ()
    assert {"web_fetch", "web_research"}.issubset(
        {tool_name for capability in discovered for tool_name in capability.tool_names}
    )


def test_discover_capabilities_supports_catalog_and_explicit_preflight(
    tmp_path,
) -> None:
    provider = CatalogProvider(model="kimi-catalog")
    catalog = discover_capabilities(provider, root=tmp_path, home=tmp_path)
    explicit = discover_capabilities(
        provider,
        root=tmp_path,
        home=tmp_path,
        capability_ids=["file.write", "mcp"],
    )

    assert [capability.id for capability in catalog] == [
        "file.read",
        "file.search",
        "file.extract_context",
        "file.write",
        "shell",
        "web.fetch",
        "web.search",
        "web.research",
        "web",
        "image.attach",
        "image.generation",
        "mcp",
    ]
    assert explicit[0].tool_names == ("edit", "write")
    assert not explicit[1].available
    assert [capability.id for capability in explicit] == ["file.write", "mcp"]
    with pytest.raises(ValueError, match="Unknown discoverable capability"):
        discover_capabilities(provider, root=tmp_path, capability_ids=["unknown"])
    with pytest.raises(TypeError, match="sequence of capability ID strings"):
        discover_capabilities(provider, root=tmp_path, capability_ids="file.read")
    with pytest.raises(ValueError, match="exactly one"):
        discover_capabilities(root=tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        discover_capabilities(provider, selection="demo", root=tmp_path)


def test_selection_discovery_owns_temporary_provider(tmp_path, monkeypatch) -> None:
    provider = CatalogProvider()
    monkeypatch.setattr(
        "yoke.ai.sdk.providers.build_builtin_provider",
        lambda selection: provider,
    )

    discovered = discover_capabilities(
        selection="demo:gpt-catalog:high",
        root=tmp_path,
        home=tmp_path,
        capability_ids=["file.read", "file.search", "file.extract_context"],
    )

    assert [capability.id for capability in discovered] == [
        "file.read",
        "file.search",
        "file.extract_context",
    ]
    assert provider.closed
