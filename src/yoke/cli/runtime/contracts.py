"""Optional runtime capabilities supported by CLI adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from yoke.agent.loop import AgentEventHandler, AgentResult
from yoke.agent.models import Message


@runtime_checkable
class UserMessageRunner(Protocol):
    """Runner opting into explicit user message delivery."""

    supports_user_message: bool

    def run(
        self,
        prompt: str,
        *,
        user_message: Message,
        on_event: AgentEventHandler | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> AgentResult: ...


@runtime_checkable
class HistoryRunner(Protocol):
    """Runner opting into transcript history delivery."""

    supports_message_history: bool

    def run(
        self,
        prompt: str,
        messages: list[Message],
        *,
        on_event: AgentEventHandler | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> AgentResult: ...


def user_message_runner(agent: object) -> UserMessageRunner | None:
    """Resolve the explicit-message adapter only when the runner opts in."""
    return (
        agent
        if isinstance(agent, UserMessageRunner) and agent.supports_user_message
        else None
    )


def history_runner(agent: object) -> HistoryRunner | None:
    """Resolve the transcript adapter only when the runner opts in."""
    return (
        agent
        if isinstance(agent, HistoryRunner) and agent.supports_message_history
        else None
    )
