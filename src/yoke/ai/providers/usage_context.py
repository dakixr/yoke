"""Explicit attribution context for provider usage metrics."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from typing import Literal

type UsageSurface = Literal["cli", "sdk"]
type SdkOperation = Literal["complete", "agent", "run_many"]
type UsageCallKind = Literal[
    "direct_completion",
    "model_iteration",
    "structured_output_retry",
    "compaction_summary",
    "branch_summary",
    "overflow_retry",
]


@dataclass(frozen=True, slots=True)
class UsageMetricContext:
    """Attribution inherited by provider calls in the current execution flow."""

    surface: UsageSurface | None = None
    session_id: str | None = None
    session_title: str | None = None
    sdk_operation: SdkOperation | None = None
    sdk_run_id: str | None = None
    call_kind: UsageCallKind | None = None

    def record_fields(self) -> dict[str, str]:
        """Return non-empty context fields for one metric record."""
        return {
            key: value
            for key, value in asdict(self).items()
            if isinstance(value, str) and value
        }


_USAGE_METRIC_CONTEXT: ContextVar[UsageMetricContext | None] = ContextVar(
    "yoke_usage_metric_context",
    default=None,
)


def current_usage_metric_context() -> UsageMetricContext:
    """Return attribution for the current task or thread."""
    return _USAGE_METRIC_CONTEXT.get() or UsageMetricContext()


@contextmanager
def usage_metric_context(
    *,
    surface: UsageSurface | None = None,
    session_id: str | None = None,
    session_title: str | None = None,
    sdk_operation: SdkOperation | None = None,
    sdk_run_id: str | None = None,
    call_kind: UsageCallKind | None = None,
) -> Iterator[UsageMetricContext]:
    """Start a surface scope or merge details into the current scope."""
    current = (
        UsageMetricContext() if surface is not None else current_usage_metric_context()
    )
    updates = {
        key: value
        for key, value in {
            "surface": surface,
            "session_id": session_id,
            "session_title": session_title,
            "sdk_operation": sdk_operation,
            "sdk_run_id": sdk_run_id,
            "call_kind": call_kind,
        }.items()
        if value is not None
    }
    active = replace(current, **updates)
    token = _USAGE_METRIC_CONTEXT.set(active)
    try:
        yield active
    finally:
        _USAGE_METRIC_CONTEXT.reset(token)
