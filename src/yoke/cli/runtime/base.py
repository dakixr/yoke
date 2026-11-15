"""Core runtime helpers for yoke CLI."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock, Thread
from typing import Protocol, runtime_checkable

from yoke.agent.compaction import TokenEstimate
from yoke.agent.loop import AgentResult
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import AgentContext, ConversationEntry, Message
from yoke.agent.protocols import AgentRunner
from yoke.agent.skills.models import ActiveSkill, SkillSpec
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.render import OutputStream, StatusIndicator
from yoke.cli.runtime.contracts import history_runner, user_message_runner
from yoke.cli.runtime.stats import (
    conversation_stats as summarize_conversation_stats,
)
from yoke.cli.runtime.stats import (
    estimate_messages_token_usage as estimate_serialized_message_tokens,
)
from yoke.session.repository import SessionRecord, SessionStore, SessionTreeIndex


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
    save_lock: RLock = field(default_factory=RLock, repr=False)
    title_worker: Thread | None = field(default=None, init=False, repr=False)
    tree_index: SessionTreeIndex = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and index loaded topology once for this session lifetime."""
        self.tree_index = SessionTreeIndex(
            self.record.conversation_entries,
            self.record.leaf_id,
        )

    def active_entry_refs(self) -> list[ConversationEntry]:
        """Return a shallow active-path snapshot for internal runtime use."""
        return list(self.tree_index.active_entry_refs())

    def active_entries(self) -> list[ConversationEntry]:
        """Return defensive active-path entries for an ownership transfer."""
        return self.tree_index.active_entries()

    def messages(self) -> list[Message]:
        """Project transcript messages from the retained active path."""
        return [
            entry.message.model_copy(deep=True)
            for entry in self.tree_index.active_entry_refs()
            if entry.kind not in {"instruction", "memory_snapshot"}
            and entry.message is not None
        ]


@dataclass(slots=True)
class ActiveSessionRef:
    """Mutable session selection shared by front-end closures."""

    value: ActiveSession


def execute_turn(
    agent: AgentRunner,
    prompt: str,
    messages: list[Message],
    *,
    stderr: OutputStream | None = None,
    indicator: EventRenderer | None = None,
    stop_requested: Callable[[], bool] | None = None,
    user_message: Message | None = None,
    active_skills: Sequence[ActiveSkill] | None = None,
    available_skills: Sequence[SkillSpec] | None = None,
    conversation_entries: Sequence[ConversationEntry] | None = None,
    after_tool_result_appended: (
        Callable[[list[Message], list[ConversationEntry]], None] | None
    ) = None,
) -> AgentResult:
    """Execute one CLI turn against the agent."""
    active_indicator = indicator or StatusIndicator(stderr or sys.stderr)
    with active_indicator:
        user_runner = user_message_runner(agent)
        if (
            user_message is not None
            and user_message.has_image_inputs()
            and user_runner is None
        ):
            raise ValueError("This agent implementation does not support image inputs.")
        if isinstance(agent, RuntimeAgent):
            if not agent.has_state:
                agent.load_conversation(
                    messages=messages if conversation_entries is None else None,
                    conversation_entries=conversation_entries,
                    available_skills=available_skills,
                    active_skills=active_skills,
                )
            return agent.run(
                prompt,
                user_message=user_message,
                on_event=active_indicator.handle_event,
                stop_requested=stop_requested,
                active_skills=active_skills,
                available_skills=available_skills,
                after_tool_result_appended=(
                    lambda context: (
                        _checkpoint_runtime_context(
                            agent,
                            context,
                            after_tool_result_appended,
                        )
                        if after_tool_result_appended is not None
                        else None
                    )
                ),
            )
        if user_message is not None and user_runner is not None:
            return user_runner.run(
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
        history_adapter = history_runner(agent)
        if history_adapter is not None:
            return history_adapter.run(
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


def _checkpoint_runtime_context(
    agent: RuntimeAgent,
    context: AgentContext,
    callback: Callable[[list[Message], list[ConversationEntry]], None],
) -> None:
    """Expose one accepted tool-result checkpoint to CLI state."""
    agent.active_skills = [
        skill.model_copy(deep=True) for skill in context.active_skills
    ]
    callback(
        list(context.messages),
        [entry.model_copy(deep=True) for entry in context.conversation_log.entries],
    )


def estimate_messages_token_usage(messages: list[Message]) -> TokenEstimate:
    """Estimate token usage from serialized message payload size."""
    return estimate_serialized_message_tokens(messages)


def conversation_stats(messages: list[Message]) -> dict[str, object]:
    """Summarize message-role and token statistics."""
    return summarize_conversation_stats(messages)
