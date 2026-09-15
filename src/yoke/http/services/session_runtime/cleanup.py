"""Single-flight cleanup of an owned agent followed by its provider."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from yoke.agent.loop.agent import RuntimeAgent

if TYPE_CHECKING:
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RetiredProvider:
    """A provider-only owner using the same single-flight cleanup as agents."""

    provider: object


class AgentCleanup:
    """Mutable state for one single-flight agent and provider cleanup."""

    def __init__(self, owner: SessionRuntimeResources, agent: object) -> None:
        self.owner = owner
        self.agent = agent
        self.agent_terminal = False
        self.provider: object | None = None
        self.failures = 0
        self.completion: Future[None] | None = None

    def attempt(self) -> bool:
        """Attempt agent and provider cleanup on the owned retirement thread."""
        if not self.agent_terminal:
            close = getattr(self.agent, "close", None)
            try:
                if callable(close):
                    close()
                self.agent_terminal = True
            except Exception:  # noqa: BLE001
                self.failures += 1
                terminal = isinstance(self.agent, RuntimeAgent) and self.agent.closed
                if self.failures == 1:
                    LOGGER.exception(
                        "Failed to close HTTP agent for session %s.",
                        self.owner.session_id,
                    )
                if not terminal:
                    return False
                self.agent_terminal = True
        if self.provider is None:
            self.provider = self.owner._remove_terminal_agent(self.agent)
        if self.provider is not None:
            close_provider = getattr(self.provider, "close", None)
            try:
                if callable(close_provider):
                    close_provider()
            except Exception:  # noqa: BLE001
                self.failures += 1
                if self.failures == 1:
                    LOGGER.exception(
                        "Failed to close an HTTP provider for session %s.",
                        self.owner.session_id,
                    )
                return False
            self.owner._remove_terminal_provider(self.provider)
            self.provider = None
        self.owner._finish_cleanup(self)
        return True
