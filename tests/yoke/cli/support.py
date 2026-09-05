from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yoke.agent.context import ContextManager
from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.main import CLIArgs
from yoke.cli.runtime import create_active_session


@dataclass
class FakeAgent:
    supports_message_history = True
    supports_user_message = False

    outputs: list[str] = field(default_factory=lambda: ["synthetic response"])
    seen_history_lengths: list[int] = field(default_factory=list)
    tool_report: ToolLoadReport | None = None
    provider: Any = None
    context_manager: ContextManager | None = None

    def run(
        self,
        prompt: str,
        messages: Sequence[Message] | None = None,
        *,
        on_event: Any = None,
        stop_requested: Any = None,
    ) -> AgentResult:
        del on_event, stop_requested
        self.seen_history_lengths.append(len(messages or []))
        output = self.outputs[
            min(len(self.seen_history_lengths) - 1, len(self.outputs) - 1)
        ]
        conversation = list(messages or [])
        conversation.append(Message.user(prompt))
        conversation.append(Message.assistant(output))
        return AgentResult(output=output, messages=conversation, iterations=1)


@dataclass
class ImageAwareAgent:
    supports_message_history = True
    supports_user_message = True

    seen_user_messages: list[Message] = field(default_factory=list)

    def run(
        self,
        prompt: str,
        messages: Sequence[Message] | None = None,
        *,
        user_message: Message | None = None,
        on_event: Any = None,
        stop_requested: Any = None,
    ) -> AgentResult:
        del on_event, stop_requested
        message = user_message or Message.user(prompt)
        self.seen_user_messages.append(message.model_copy(deep=True))
        conversation = list(messages or [])
        conversation.append(message.model_copy(deep=True))
        conversation.append(Message.assistant("image response"))
        return AgentResult(
            output="image response",
            messages=conversation,
            iterations=1,
        )


class ProviderConfig:
    model = "gpt-test"


class FakeProvider:
    config = ProviderConfig()
    supports_image_inputs = False
    max_images_per_message = None


class CaptureStream(io.StringIO):
    def isatty(self) -> bool:
        return False


def active_session_for(root: Path):
    return create_active_session(CLIArgs(root=str(root)), root=root)
