"""LLM-generated titles for Yoke CLI sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from yoke.ai.providers.usage_context import UsageMetricContext
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.session import fallback_session_title
from yoke.session.title import generate_session_title as generate_session_title


@contextmanager
def session_usage_metric_context(
    session: ActiveSession,
    prompt: str,
) -> Iterator[UsageMetricContext]:
    """Attribute provider calls to the current CLI session."""
    title = session.title or fallback_session_title(prompt)
    with usage_metric_context(
        surface="cli",
        session_id=session.id,
        session_title=title,
    ) as context:
        yield context
