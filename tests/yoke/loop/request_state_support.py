"""Observe working epoch state without an unused provider dispatch protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop.cache_scope import conversation_cache_scope
from yoke.agent.loop.requests import ModelRequests
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.models import AgentContext
from yoke.ai.providers.usage_context import current_usage_metric_context


@dataclass(frozen=True, slots=True)
class RequestState:
    """The same metadata derived from context by the former request wrapper."""

    cache_scope: str
    response_continuity: Literal["continue", "reset"]


def provider_request_state(provider: object) -> RequestState:
    """Read the actual working context at the provider call, before mutation."""
    context = cast(AgentContext, getattr(provider, "oracle_context"))
    summary = current_usage_metric_context().call_kind == "compaction_summary"
    return RequestState(
        cache_scope=conversation_cache_scope(context),
        response_continuity="reset"
        if context.provider_epoch_reset and not summary
        else "continue",
    )


def capture_request_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attach a context observer without changing any request or return value."""
    original = ModelRequests.__init__

    def observe(
        self: ModelRequests,
        runtime: RuntimeAgent,
        context: AgentContext,
        on_event: AgentEventHandler | None = None,
    ) -> None:
        original(self, runtime, context, on_event)
        setattr(runtime.provider, "oracle_context", context)

    monkeypatch.setattr(ModelRequests, "__init__", observe)
