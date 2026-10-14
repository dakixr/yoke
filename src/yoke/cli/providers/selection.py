"""Strict thinking-effort selection shared by CLI entry points."""

from __future__ import annotations

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import default_reasoning_effort_for_model
from yoke.ai.providers.resolution import parse_provider_ref
from yoke.ai.providers.resolution import ProviderRef


def parse_cli_model_ref(value: str) -> ProviderRef:
    """Parse a CLI selection, rejecting an explicitly empty thinking effort."""
    ref = parse_provider_ref(value)
    if value.count(":") == 2 and ref.reasoning_effort is None:
        raise ValueError(
            "Expected a non-empty thinking effort in "
            "`provider-name:model-name:thinking-effort`."
        )
    return ref


def resolve_cli_reasoning_effort(
    model: ProviderModelInfo,
    requested: str | None,
    *,
    provider_name: str,
) -> str | None:
    """Use a model default only when no thinking effort was supplied."""
    if requested is None:
        return default_reasoning_effort_for_model(model)
    normalized = requested.strip().lower()
    if normalized not in model.thinking_levels:
        supported = (
            ", ".join(model.thinking_levels)
            or "no configurable levels; omit the thinking effort"
        )
        raise ValueError(
            f"Unsupported thinking effort {requested!r} for model "
            f"{provider_name + ':' + model.id!r}. Supported: {supported}."
        )
    return normalized
