# ruff: noqa: D100,D103,S101

from __future__ import annotations

from yoke.agent.compaction import TokenEstimate
from yoke.agent.models import TokenUsage
from yoke.agent.usage import effective_usage_accounting
from yoke.ai.providers.usage import parse_token_usage


def test_parse_token_usage_normalizes_chat_completion_shape() -> None:
    usage = parse_token_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 30,
            "total_tokens": 130,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
        provider_name="codex",
        model_id="gpt-test",
    )

    assert usage is not None
    assert usage.provider_name == "codex"
    assert usage.model_id == "gpt-test"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 30
    assert usage.total_tokens == 130
    assert usage.reasoning_tokens == 20
    assert usage.cached_input_tokens == 60


def test_parse_token_usage_ignores_invalid_or_empty_payloads() -> None:
    assert parse_token_usage(None) is None
    assert parse_token_usage({"input_tokens": -1}) is None
    assert parse_token_usage({"input_tokens": True}) is None


def test_effective_usage_accounting_falls_back_to_estimate() -> None:
    accounting = effective_usage_accounting(
        TokenEstimate(input_tokens=40, total_with_reserve=50),
        latest_usage=None,
    )

    assert accounting.source == "estimate"
    assert accounting.input_tokens == 40
    assert accounting.total_with_reserve == 50
    assert accounting.provider_reported_input_tokens is None


def test_effective_usage_accounting_prefers_provider_input_tokens() -> None:
    accounting = effective_usage_accounting(
        TokenEstimate(input_tokens=40, total_with_reserve=50),
        latest_usage=TokenUsage(
            input_tokens=100,
            output_tokens=12,
            reasoning_tokens=8,
            total_tokens=112,
            cached_input_tokens=20,
        ),
    )

    assert accounting.source == "provider"
    assert accounting.input_tokens == 100
    assert accounting.total_with_reserve == 110
    assert accounting.estimated_input_tokens == 40
    assert accounting.provider_reported_input_tokens == 100
    assert accounting.reasoning_tokens == 8


def test_effective_usage_accounting_ignores_implausible_stale_usage() -> None:
    accounting = effective_usage_accounting(
        TokenEstimate(input_tokens=2_000, total_with_reserve=3_000),
        latest_usage=TokenUsage(input_tokens=75_000),
    )

    assert accounting.source == "estimate"
    assert accounting.input_tokens == 2_000
    assert accounting.total_with_reserve == 3_000
    assert accounting.provider_reported_input_tokens is None
