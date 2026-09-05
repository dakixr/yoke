"""Terminal-close checks for short-lived HTTP-owned agents."""

from __future__ import annotations

import logging

from yoke.agent.loop.agent import RuntimeAgent
from yoke.http.services.session_runtime.reaper import retire_resource


LOGGER = logging.getLogger(__name__)


class _OwnedAgentClose:
    def __init__(
        self,
        agent: object,
        *,
        surviving_provider: object | None,
        description: str,
    ) -> None:
        self.agent = agent
        self.provider = (
            getattr(agent, "provider", None)
            if isinstance(agent, RuntimeAgent)
            else None
        )
        self.surviving_provider = surviving_provider
        self.description = description
        self.agent_terminal = False
        self.provider_terminal = (
            self.provider is None or self.provider is surviving_provider
        )
        self.failures = 0

    def attempt(self) -> bool:
        """Run one cleanup attempt and report whether ownership may be dropped."""
        if not self.agent_terminal:
            close_agent = getattr(self.agent, "close", None)
            try:
                if callable(close_agent):
                    close_agent()
                self.agent_terminal = True
            except Exception:  # noqa: BLE001
                self.failures += 1
                terminal = isinstance(self.agent, RuntimeAgent) and self.agent.closed
                if self.failures == 1:
                    LOGGER.exception("Failed to close %s.", self.description)
                if not terminal:
                    return False
                self.agent_terminal = True
        if self.provider_terminal:
            return True
        close_provider = getattr(self.provider, "close", None)
        try:
            if callable(close_provider):
                close_provider()
            self.provider_terminal = True
            return True
        except Exception:  # noqa: BLE001
            self.failures += 1
            if self.failures == 1:
                LOGGER.exception("Failed to close provider for %s.", self.description)
            return False


def close_owned_agent(
    agent: object | None,
    *,
    surviving_provider: object | None = None,
    description: str = "HTTP-owned agent",
) -> bool:
    """Close now when possible, or transfer retained ownership to the reaper.

    ``False`` means the agent or provider is nonterminal and remains strongly
    owned by the daemon retirement worker. In particular, ``RuntimeAgent.closed``
    being false after ``close()`` raises never permits provider closure.
    """
    if agent is None:
        return True
    cleanup = _OwnedAgentClose(
        agent,
        surviving_provider=surviving_provider,
        description=description,
    )
    if cleanup.attempt():
        return True
    retire_resource(cleanup.attempt)
    return False
