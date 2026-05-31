"""Core runtime helpers for yoke CLI."""

from __future__ import annotations

import sys
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol
from typing import cast
from typing import runtime_checkable

from yoke.agent.compaction import TokenEstimate
from yoke.agent.loop import AgentResult
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.protocols import AgentRunner
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.render import OutputStream
from yoke.cli.render import StatusIndicator
from yoke.cli.runtime.stats import (
    conversation_stats as summarize_conversation_stats,
)
from yoke.cli.runtime.stats import (
    estimate_messages_token_usage as estimate_serialized_message_tokens,
)
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore


@runtime_checkable
class ToolReportAgent(Protocol):
    """Protocol for agents that expose tool discovery reports."""

    tool_report: ToolLoadReport | None


class EventRenderer(Protocol):
    """Protocol for runtime event renderers."""

    def __enter__(self) -> EventRenderer:
        """Enter the renderer context."""
        ...

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Exit the renderer context."""

    def handle_event(self, event: str, payload: dict[str, object]) -> None:
        """Handle a runtime event."""


@dataclass(slots=True)
class ActiveSession:
    """Current CLI session state."""

    id: str
    root: Path
    store: SessionStore
    record: SessionRecord
    title: str | None = None


def execute_turn(
    agent: AgentRunner,
    prompt: str,
    messages: list[Message],
    *,
    stderr: OutputStream | None = None,
    indicator: EventRenderer | None = None,
    stop_requested: Callable[[], bool] | None = None,
    user_message: Message | None = None,
    active_skills: Sequence[object] | None = None,
    available_skills: Sequence[object] | None = None,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> AgentResult:
    """Execute one CLI turn against the agent."""
    active_indicator = indicator or StatusIndicator(stderr or sys.stderr)
    with active_indicator:
        if (
            user_message is not None
            and user_message.has_image_inputs()
            and not getattr(agent, "supports_user_message", False)
        ):
            raise ValueError("This agent implementation does not support image inputs.")
        if isinstance(agent, RuntimeAgent):
            if not agent.has_state:
                agent.load_conversation(
                    messages=messages if conversation_entries is None else None,
                    conversation_entries=conversation_entries,
                    available_skills=cast(Sequence[SkillSpec] | None, available_skills),
                    active_skills=cast(Sequence[ActiveSkill] | None, active_skills),
                )
            return agent.run(
                prompt,
                user_message=user_message,
                on_event=active_indicator.handle_event,
                stop_requested=stop_requested,
                active_skills=cast(Sequence[ActiveSkill] | None, active_skills),
                available_skills=cast(Sequence[SkillSpec] | None, available_skills),
            )
        if user_message is not None and getattr(agent, "supports_user_message", False):
            return cast(Any, agent).run(
                prompt,
                user_message=user_message,
                on_event=active_indicator.handle_event,
                stop_requested=stop_requested,
            )
        if user_message is not None and user_message.has_image_inputs():
            raise TypeError(
                "This agent implementation does not support explicit user "
                "messages. Set supports_user_message=True and accept "
                "user_message=... in run()."
            )
        if getattr(agent, "supports_message_history", False):
            return cast(Any, agent).run(
                prompt,
                messages,
                on_event=active_indicator.handle_event,
                stop_requested=stop_requested,
            )
        return agent.run(
            prompt,
            on_event=active_indicator.handle_event,
            stop_requested=stop_requested,
        )


def estimate_messages_token_usage(messages: list[Message]) -> TokenEstimate:
    """Estimate token usage from serialized message payload size."""
    return estimate_serialized_message_tokens(messages)


def conversation_stats(messages: list[Message]) -> dict[str, object]:
    """Summarize message-role and token statistics."""
    return summarize_conversation_stats(messages)
