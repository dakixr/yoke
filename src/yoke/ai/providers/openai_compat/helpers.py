"""Shared helpers for OpenAI-compatible providers."""

from __future__ import annotations

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import cloned_model_catalog


def build_model_catalog(
    *models: ProviderModelInfo,
) -> tuple[ProviderModelInfo, ...]:
    """Build a validated immutable provider model catalog."""
    return tuple(cloned_model_catalog(models))


def thinking_levels_for_reasoning_effort(
    reasoning_effort: str | None,
) -> tuple[str, ...]:
    """Map a configured reasoning effort to supported thinking levels."""
    if reasoning_effort is None:
        return (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
    normalized = reasoning_effort.strip().lower()
    return (
        (normalized,)
        if normalized
        else (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
    )
