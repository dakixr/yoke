"""Agent-level token usage helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from yoke.agent.compaction import TokenEstimate
from yoke.agent.models import TokenUsage

UsageAccountingSource = Literal["provider", "estimate"]


@dataclass(frozen=True, slots=True)
class UsageAccounting:
    """Effective token accounting for a provider request."""

    input_tokens: int
    total_with_reserve: int
    estimated_input_tokens: int
    estimated_total_with_reserve: int
    provider_reported_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    source: UsageAccountingSource = "estimate"


def effective_usage_accounting(
    estimate: TokenEstimate,
    *,
    latest_usage: TokenUsage | None,
) -> UsageAccounting:
    """Prefer provider-reported input tokens when available."""
    if (
        latest_usage is None
        or latest_usage.input_tokens is None
        or not _is_plausible_current_usage(
            latest_usage.input_tokens,
            estimate.input_tokens,
        )
    ):
        return UsageAccounting(
            input_tokens=estimate.input_tokens,
            total_with_reserve=estimate.total_with_reserve,
            estimated_input_tokens=estimate.input_tokens,
            estimated_total_with_reserve=estimate.total_with_reserve,
        )
    reported_input_tokens = latest_usage.input_tokens
    effective_input_tokens = max(estimate.input_tokens, reported_input_tokens)
    reserve_tokens = max(0, estimate.total_with_reserve - estimate.input_tokens)
    return UsageAccounting(
        input_tokens=effective_input_tokens,
        total_with_reserve=effective_input_tokens + reserve_tokens,
        estimated_input_tokens=estimate.input_tokens,
        estimated_total_with_reserve=estimate.total_with_reserve,
        provider_reported_input_tokens=latest_usage.input_tokens,
        output_tokens=latest_usage.output_tokens,
        reasoning_tokens=latest_usage.reasoning_tokens,
        total_tokens=latest_usage.total_tokens,
        cached_input_tokens=latest_usage.cached_input_tokens,
        source=(
            "provider" if reported_input_tokens >= estimate.input_tokens else "estimate"
        ),
    )


def _is_plausible_current_usage(
    reported_input_tokens: int,
    estimated_input_tokens: int,
) -> bool:
    if reported_input_tokens <= 0:
        return False
    if estimated_input_tokens <= 0:
        return True
    return reported_input_tokens <= max(estimated_input_tokens * 20, 8_000)


def compact_usage_payload(usage: TokenUsage | None) -> dict[str, int] | None:
    """Return a small metadata-safe usage summary."""
    if usage is None:
        return None
    payload = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
    }
    compact = {key: value for key, value in payload.items() if value is not None}
    return compact or None
