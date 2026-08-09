from __future__ import annotations

# ruff: noqa: ANN001, ANN201, D100, D103, S101

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import (
    compatible_reasoning_effort_for_model,
)
from yoke.ai.providers.model_selection import (
    default_reasoning_effort_for_model,
)
from yoke.ai.providers.model_selection import (
    set_config_model_from_catalog,
)
import pytest
from pydantic import ValidationError


class _Config:
    def __init__(self) -> None:
        self.model: str | None = None
        self.reasoning_effort: str | None = None


def test_default_reasoning_effort_for_model_prefers_model_default() -> None:
    model = ProviderModelInfo(
        id="gpt-test",
        display_name="GPT Test",
        context_window_tokens=1000,
        thinking_levels=("low", "high", "xhigh"),
        default_thinking_level="xhigh",
    )

    assert default_reasoning_effort_for_model(model) == "xhigh"


def test_compatible_reasoning_effort_falls_back_to_model_default() -> None:
    model = ProviderModelInfo(
        id="glm-5.2",
        display_name="GLM-5.2",
        context_window_tokens=1_000_000,
        thinking_levels=("none", "thinking"),
        default_thinking_level="thinking",
    )

    assert compatible_reasoning_effort_for_model(model, "none") == "none"
    assert compatible_reasoning_effort_for_model(model, "medium") == "thinking"
    assert compatible_reasoning_effort_for_model(model, None) == "thinking"


def test_model_catalog_rejects_default_outside_supported_levels() -> None:
    with pytest.raises(ValidationError, match="default_thinking_level"):
        ProviderModelInfo(
            id="broken",
            display_name="Broken",
            context_window_tokens=1_000,
            thinking_levels=("none", "thinking"),
            default_thinking_level="medium",
        )


def test_set_config_model_applies_model_default_reasoning() -> None:
    config = _Config()
    models = (
        ProviderModelInfo(
            id="gpt-5.4-mini",
            display_name="GPT-5.4 Mini",
            context_window_tokens=400_000,
            thinking_levels=("none", "low", "medium", "high", "xhigh"),
            default_thinking_level="xhigh",
        ),
        ProviderModelInfo(
            id="gpt-5.5",
            display_name="GPT-5.5",
            context_window_tokens=400_000,
            thinking_levels=("none", "low", "medium", "high", "xhigh"),
            default_thinking_level="low",
        ),
    )

    set_config_model_from_catalog(
        config,
        models,
        provider_name="codex",
        model_id="gpt-5.4-mini",
    )
    assert config.model == "gpt-5.4-mini"
    assert config.reasoning_effort == "xhigh"

    set_config_model_from_catalog(
        config,
        models,
        provider_name="codex",
        model_id="gpt-5.5",
    )
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "low"

    set_config_model_from_catalog(
        config,
        models,
        provider_name="codex",
        model_id="gpt-5.4-mini",
        reasoning_effort="max",
    )
    assert config.model == "gpt-5.4-mini"
    assert config.reasoning_effort == "xhigh"
