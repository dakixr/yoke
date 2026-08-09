"""Regression tests for dynamic tool instructions across runtime forks."""

# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import ReadTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.ai.providers.base import Provider


class RecordingProvider(Provider):
    """Record the final provider payload."""

    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        """Record messages and return a final response."""
        del tools
        self.messages = messages
        return Message.assistant("done")


def tool_factory(
    context: ToolRegistrationContext,
) -> ToolRegistrationResult:
    """Register one tool with one system instruction."""
    return ToolRegistrationResult(
        tools=[ReadTool.bind(root=context.root)],
        system_messages=[Message.system("tool guidance")],
    )


def test_runtime_fork_does_not_duplicate_tool_system_messages(
    tmp_path: Path,
) -> None:
    """Re-register tool guidance exactly once in a turn runtime."""
    provider = RecordingProvider()
    primary = RuntimeAgent(
        provider=provider,
        tools=[],
        tool_factory=tool_factory,
        tool_root=tmp_path,
        context_manager=ContextManager(instructions=[Message.system("base guidance")]),
    )

    primary.run("first turn")
    forked = primary.fork()
    forked.run("second turn")

    system_contents = [
        message.text_content()
        for message in provider.messages
        if message.role == "system"
    ]
    assert system_contents == ["base guidance", "tool guidance"]
    primary.close()
    forked.close()
