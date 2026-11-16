"""Legacy call-shape adapters for explicitly injected CLI test doubles.

Production execution always uses RuntimeAgent. There is no custom-runner
protocol. These adapters keep existing injected front-end tests independent of
provider construction while the CLI's in-process implementation is retired.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from yoke.agent.loop import AgentResult


def user_message_runner(agent: object) -> Callable[..., AgentResult] | None:
    """Return an explicitly opted-in multimodal test-double call."""
    run = getattr(agent, "run", None)
    if callable(run) and getattr(agent, "supports_user_message", False):
        return cast(Callable[..., AgentResult], run)
    return None


def history_runner(agent: object) -> Callable[..., AgentResult] | None:
    """Return an explicitly opted-in transcript test-double call."""
    run = getattr(agent, "run", None)
    if callable(run) and getattr(agent, "supports_message_history", False):
        return cast(Callable[..., AgentResult], run)
    return None
