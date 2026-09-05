from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from pathlib import Path

from yoke.agent.capabilities import create_builtin_capabilities
from yoke.agent.capabilities import known_builtin_capability_ids
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools.context import ModelIdentity
from yoke.ai.providers.base import Provider


class ProviderStub(Provider):
    provider_name = "test"
    max_images_per_message = None
    supports_image_inputs = False

    class Config:
        """Provider config stub."""

        model = "claude-test"

    def __init__(self) -> None:
        self.config = self.Config()

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")


def context(tmp_path: Path, model_id: str) -> ToolRegistrationContext:
    provider = ProviderStub()
    provider.config.model = model_id
    return ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=provider,
        model=ModelIdentity(provider_name="test", model_id=model_id),
    )


def tool_names_for(
    capabilities,
    capability_id: str,
) -> set[str]:
    return {
        tool.name
        for capability in capabilities
        if capability.capability_id == capability_id
        for tool in capability.tools
    }


def test_capability_tools_receive_runtime_context(tmp_path) -> None:
    registrations = create_builtin_capabilities(context(tmp_path, "gpt-test"))
    tool = next(
        tool
        for capability in registrations
        if capability.capability_id == "file.write"
        for tool in capability.tools
    )

    assert isinstance(tool, LocalTool)
    assert tool._context["runtime_context"] is not None


def test_builtin_capabilities_have_one_file_read_interface() -> None:
    assert known_builtin_capability_ids() == {
        "file.read",
        "file.search",
        "file.write",
        "image.attach",
        "image.generate",
        "mcp",
        "shell",
        "web.fetch",
        "web.research",
        "web.search",
    }


def test_file_read_capability_registers_both_read_tools(tmp_path) -> None:
    registrations = create_builtin_capabilities(context(tmp_path, "gpt-test"))

    assert tool_names_for(registrations, "file.read") == {
        "extract_file_context",
        "read",
    }
