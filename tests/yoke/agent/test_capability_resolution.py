from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from pathlib import Path
from types import SimpleNamespace

import pytest

from yoke.agent.capabilities import create_builtin_capabilities
from yoke.agent.capabilities import resolve_builtin_capability
from yoke.agent.capabilities.base import CapabilityRegistration
from yoke.agent.capabilities import builtins as builtins_module
from yoke.agent.models import Message
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools.context import ModelIdentity
from yoke.ai import Agent
from yoke.ai.providers.base import Provider
from yoke.ai.sdk.defaults import default_coding_agent_config
from yoke.ai.sdk.runtime import bind_agent_tools_result


class ProjectionProvider(Provider):
    max_images_per_message = None

    def __init__(
        self,
        *,
        provider_name: str,
        model: str,
        supports_images: bool,
        supports_generation: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.config = SimpleNamespace(model=model)
        self.supports_image_inputs = supports_images
        self.supports_image_generation = supports_generation
        self.close_calls = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")

    def generate_image(self, *, prompt: str) -> str:
        del prompt
        return ""

    def close(self) -> None:
        self.close_calls += 1


def registration_context(
    tmp_path: Path,
    provider: ProjectionProvider,
) -> ToolRegistrationContext:
    return ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=provider,
        model=ModelIdentity(
            provider_name=provider.provider_name,
            model_id=provider.config.model,
            supports_image_inputs=provider.supports_image_inputs,
        ),
    )


def registration_projection(registration: CapabilityRegistration) -> object:
    return (
        registration.capability_id,
        [tool.name for tool in registration.tools],
        [tool.to_definition() for tool in registration.tools],
        [message.model_dump(mode="json") for message in registration.system_messages],
    )


def test_targeted_resolver_does_not_resolve_unselected_or_unknown_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []

    class ProbeCapability:
        def __init__(self, capability_id: str) -> None:
            self.capability_id = capability_id

        def resolve(
            self,
            context: ToolRegistrationContext,
        ) -> CapabilityRegistration:
            del context
            resolved.append(self.capability_id)
            return CapabilityRegistration(self.capability_id, ())

    capabilities = tuple(
        ProbeCapability(capability_id)
        for capability_id in ("before", "target", "after")
    )
    monkeypatch.setattr(
        builtins_module,
        "builtin_capabilities",
        lambda: capabilities,
    )
    provider = ProjectionProvider(
        provider_name="test",
        model="gpt-test",
        supports_images=False,
    )
    context = registration_context(tmp_path, provider)

    registration = resolve_builtin_capability("target", context)

    assert registration.capability_id == "target"
    assert resolved == ["target"]

    resolved.clear()
    with pytest.raises(ValueError, match="Unknown built-in tool capability: missing"):
        resolve_builtin_capability("missing", context)
    assert resolved == []


@pytest.mark.parametrize(
    ("provider_name", "model", "supports_images", "supports_generation"),
    [
        ("test", "gpt-test", False, False),
        ("test", "claude-test", True, False),
        ("codex", "gpt-image-test", True, True),
    ],
)
def test_targeted_resolution_matches_full_inventory_for_every_capability(
    tmp_path: Path,
    provider_name: str,
    model: str,
    supports_images: bool,
    supports_generation: bool,
) -> None:
    provider = ProjectionProvider(
        provider_name=provider_name,
        model=model,
        supports_images=supports_images,
        supports_generation=supports_generation,
    )
    context = registration_context(tmp_path, provider)
    full_inventory = create_builtin_capabilities(context)

    assert [
        registration_projection(resolve_builtin_capability(item.capability_id, context))
        for item in full_inventory
    ] == [registration_projection(item) for item in full_inventory]

    selected_ids = [item.capability_id for item in reversed(full_inventory)]
    selected = bind_agent_tools_result(
        selected_ids,
        root=tmp_path,
        provider=provider,
        enable_skill_tool=False,
        registration_context=context,
    )
    expected = [
        registration
        for capability_id in selected_ids
        for registration in full_inventory
        if registration.capability_id == capability_id
    ]
    assert [tool.name for tool in selected.tools] == [
        tool.name for registration in expected for tool in registration.tools
    ]
    assert [tool.to_definition() for tool in selected.tools] == [
        tool.to_definition() for registration in expected for tool in registration.tools
    ]
    assert [
        message.model_dump(mode="json") for message in selected.system_messages
    ] == [
        message.model_dump(mode="json")
        for registration in expected
        for message in registration.system_messages
    ]


def test_default_sdk_agent_does_not_construct_unselected_mcp_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedMcpManager:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("default SDK tools must not construct MCP resources")

    monkeypatch.setattr(builtins_module, "LazyMcpManager", UnexpectedMcpManager)
    provider = ProjectionProvider(
        provider_name="test",
        model="gpt-test",
        supports_images=False,
    )

    agent = Agent(
        provider=provider,
        config=default_coding_agent_config(tmp_path),
    )
    agent.close()

    assert provider.close_calls == 1
