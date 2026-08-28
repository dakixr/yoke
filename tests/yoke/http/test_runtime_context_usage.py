from __future__ import annotations

# ruff: noqa: D100,D103,S101

from yoke.http.services.runtime_context_usage import provider_response_context_usage
from yoke.http.services.runtime_context_usage import public_context_usage


def test_public_context_usage_allows_accounting_but_not_arbitrary_fields() -> None:
    projected = public_context_usage(
        {
            "input_tokens": 12_000,
            "max_total_tokens": 100_000,
            "usage_percent": 12,
            "reason": "tool_results",
            "api_key": "secret",
            "nested": {"token": "secret"},
        }
    )
    assert projected == {
        "input_tokens": 12_000,
        "max_total_tokens": 100_000,
        "usage_percent": 12,
        "reason": "tool_results",
    }


def test_provider_response_usage_uses_existing_reported_counts() -> None:
    projected = provider_response_context_usage(
        {
            "iteration": 3,
            "usage": {
                "input_tokens": 64_000,
                "output_tokens": 1_200,
                "reasoning_tokens": 900,
                "total_tokens": 65_200,
                "cached_input_tokens": 48_000,
                "cache_creation_input_tokens": 3_000,
            },
        },
        max_total_tokens=200_000,
    )
    assert projected == {
        "reason": "provider_response",
        "input_tokens": 64_000,
        "provider_reported_input_tokens": 64_000,
        "max_total_tokens": 200_000,
        "usage_percent": 32,
        "accounting_source": "provider",
        "iteration": 3,
        "output_tokens": 1_200,
        "reasoning_tokens": 900,
        "provider_reported_total_tokens": 65_200,
        "cached_input_tokens": 48_000,
        "cache_creation_input_tokens": 3_000,
    }
