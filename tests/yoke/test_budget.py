from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

import pytest

from yoke.agent.budget import resolve_provider_compaction_budget
from yoke.ai.providers.base import ProviderModelInfo


class CatalogProvider:
    provider_name = "test"

    def __init__(self, context_window_tokens: int) -> None:
        self.model_info = ProviderModelInfo(
            id="test-model",
            display_name="Test model",
            context_window_tokens=context_window_tokens,
            thinking_levels=("none",),
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return [self.model_info]

    def current_model_id(self) -> str:
        return self.model_info.id

    def current_model_info(self) -> ProviderModelInfo:
        return self.model_info

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        del model_id, reasoning_effort


def test_default_provider_budget_uses_separate_compaction_limits() -> None:
    policy = resolve_provider_compaction_budget(CatalogProvider(400_000)).policy

    assert policy.handoff_target_tokens == 12_000
    assert policy.recent_user_tokens == 20_000


@pytest.mark.parametrize("context_window_tokens", [1_000, 10_000, 200_000])
def test_scaled_provider_budget_keeps_user_limit_above_handoff_target(
    context_window_tokens: int,
) -> None:
    policy = resolve_provider_compaction_budget(
        CatalogProvider(context_window_tokens)
    ).policy

    assert policy.handoff_target_tokens < policy.recent_user_tokens
