"""Shared helpers for provider model catalog selection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from typing import cast

from yoke.ai.providers.base import ProviderModelInfo


class UnknownModelError(ValueError):
    """A requested model is absent from a provider's advertised catalog."""

    def __init__(
        self,
        provider_name: str,
        model_id: str,
        available_models: Sequence[str],
    ) -> None:
        self.provider_name = provider_name
        self.model_id = model_id
        self.available_models = tuple(sorted(available_models))
        choices = ", ".join(self.available_models) or "none"
        super().__init__(
            f"Unknown model {model_id!r} for provider "
            f"{provider_name!r}. Available: {choices}."
        )


def default_reasoning_effort_for_model(
    model: ProviderModelInfo,
) -> str | None:
    """Return the model-specific default thinking level when available."""
    if model.default_thinking_level is not None:
        return model.default_thinking_level
    if "medium" in model.thinking_levels:
        return "medium"
    if model.thinking_levels:
        return model.thinking_levels[0]
    return None


def compatible_reasoning_effort_for_model(
    model: ProviderModelInfo,
    requested: str | None,
) -> str | None:
    """Return a supported requested effort or the model's declared default."""
    normalized = requested.strip().lower() if requested is not None else None
    if normalized in model.thinking_levels:
        return normalized
    return default_reasoning_effort_for_model(model)


def cloned_model_catalog(
    models: Sequence[ProviderModelInfo],
) -> list[ProviderModelInfo]:
    """Return a deep-copied provider model catalog."""
    return [model.model_copy(deep=True) for model in models]


def current_model_id_from_config(config: object) -> str | None:
    """Return the normalized configured model id when present."""
    model = getattr(config, "model", None)
    if not isinstance(model, str):
        return None
    normalized = model.strip()
    return normalized or None


def current_model_info_from_catalog(
    config: object,
    models: Sequence[ProviderModelInfo],
) -> ProviderModelInfo | None:
    """Return provider model metadata for the configured model."""
    current_model = current_model_id_from_config(config)
    if current_model is None:
        return None
    for model in models:
        if model.id == current_model:
            return model.model_copy(deep=True)
    return None


def set_config_model_from_catalog(
    config: object,
    models: Sequence[ProviderModelInfo],
    *,
    provider_name: str,
    model_id: str,
    reasoning_effort: str | None = None,
) -> None:
    """Set a catalog model, falling back to its default thinking level."""
    normalized_model = model_id.strip()
    if not normalized_model:
        raise ValueError("model_id must be a non-empty string")
    available = {model.id: model for model in models}
    selected = available.get(normalized_model)
    if selected is None:
        raise UnknownModelError(provider_name, normalized_model, tuple(available))
    if hasattr(config, "reasoning_effort"):
        cast_config = cast(Any, config)
        cast_config.reasoning_effort = compatible_reasoning_effort_for_model(
            selected,
            reasoning_effort,
        )
    if not hasattr(config, "model"):
        raise ValueError("Provider config does not expose a mutable model")
    cast(Any, config).model = normalized_model
