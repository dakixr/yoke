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


@pytest.mark.parametrize(
    ("context_window_tokens", "reserved", "recent", "handoff"),
    [
        (1_000, 500, 500, 499),
        (10_000, 5_000, 4_000, 2_000),
        (200_000, 32_000, 22_000, 12_000),
    ],
)
def test_scaled_provider_budget_preserves_ratios_and_small_window_limits(
    context_window_tokens: int,
    reserved: int,
    recent: int,
    handoff: int,
) -> None:
    policy = resolve_provider_compaction_budget(
        CatalogProvider(context_window_tokens)
    ).policy

    assert policy.max_total_tokens == context_window_tokens
    assert policy.reserved_output_tokens == reserved
    assert policy.keep_recent_tokens == recent
    assert policy.recent_user_tokens == recent
    assert policy.handoff_target_tokens == handoff
