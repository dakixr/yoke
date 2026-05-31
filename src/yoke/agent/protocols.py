"""Agent-facing protocols for applications that run agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from yoke.agent.loop import AgentEventHandler
from yoke.agent.loop import AgentResult


class AgentRunner(Protocol):
    """Protocol for running one agent turn.

    Custom runners may opt into transcript history and explicit user-message
    delivery by setting ``supports_message_history`` or
    ``supports_user_message`` to ``True``.
    """

    supports_message_history: bool = False
    supports_user_message: bool = False

    def run(
        self,
        prompt: str,
        *,
        on_event: AgentEventHandler | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> AgentResult:
        """Run one turn."""
        ...
