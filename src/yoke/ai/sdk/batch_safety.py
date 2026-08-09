"""Ownership, retry, and observation safety for SDK batches."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from yoke.ai.sdk.agent import Agent
from yoke.ai.sdk.observability import BoundObserver
from yoke.ai.sdk.observability import notify_observers

type RetryPredicate = Callable[[Exception], bool]


class AgentRegistrationError(ValueError):
    """Reject an unsafe factory result and control candidate cleanup."""

    def __init__(self, message: str, *, close_candidate: bool) -> None:
        super().__init__(message)
        self.close_candidate = close_candidate


async def prepare_retry(
    error: Exception,
    *,
    should_retry: RetryPredicate | None,
    retry_delay: float,
    observer: BoundObserver | None,
) -> tuple[bool, Exception]:
    """Apply retry policy, report policy errors, and wait if requested."""
    retry, decision_error = _retry_decision(error, should_retry)
    if decision_error is not error:
        emit_batch_error(observer, decision_error, stage="retry policy")
    if retry and retry_delay:
        await asyncio.sleep(retry_delay)
    return retry, decision_error


async def register_agent(
    agent: Agent,
    used_agents: set[Agent],
    used_providers: dict[int, object],
    lock: asyncio.Lock,
) -> AgentRegistrationError | None:
    """Reject factories that reuse agents or provider instances."""
    async with lock:
        if agent in used_agents:
            return AgentRegistrationError(
                "agent_factory must return a fresh Agent for every attempt",
                close_candidate=False,
            )
        if agent.closed:
            return AgentRegistrationError(
                "agent_factory returned a closed Agent",
                close_candidate=True,
            )
        provider_identity = id(agent.provider)
        existing = used_providers.get(provider_identity)
        if existing is agent.provider:
            return AgentRegistrationError(
                "agent_factory must return an Agent with a fresh Provider "
                "for every attempt",
                close_candidate=True,
            )
        used_agents.add(agent)
        used_providers[provider_identity] = agent.provider
        return None


def emit_batch_error(
    observer: BoundObserver | None,
    error: BaseException,
    *,
    stage: str,
) -> None:
    """Emit one labeled batch-attempt failure."""
    if observer is None:
        return
    notify_observers(
        (observer,),
        "batch_attempt_error",
        {
            "stage": stage,
            "error": str(error),
            "error_type": type(error).__name__,
        },
    )


async def close_attempt_agent(
    agent: Agent | None, close_candidate: bool
) -> Exception | None:
    """Close an owned attempt agent and return a cleanup error."""
    if agent is None or not close_candidate:
        return None
    try:
        await agent.aclose()
    except Exception as exc:
        return exc
    return None


def _retry_decision(
    error: Exception, predicate: RetryPredicate | None
) -> tuple[bool, Exception]:
    if predicate is None:
        return True, error
    try:
        return predicate(error), error
    except Exception as exc:
        return False, exc
