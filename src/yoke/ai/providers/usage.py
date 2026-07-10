"""Provider token usage normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping

from yoke.agent.models import TokenUsage
from yoke.agent.models import TokenUsageDetails


def parse_token_usage(
    raw: object,
    *,
    provider_name: str | None = None,
    model_id: str | None = None,
) -> TokenUsage | None:
    """Normalize provider-specific usage payloads into TokenUsage."""
    if not isinstance(raw, Mapping):
        return None
    raw_dict = dict(raw)
    input_details = _details(
        _mapping_value(raw_dict, "input_tokens_details")
        or _mapping_value(raw_dict, "prompt_tokens_details")
    )
    output_details = _details(
        _mapping_value(raw_dict, "output_tokens_details")
        or _mapping_value(raw_dict, "completion_tokens_details")
    )
    reasoning_tokens = _int_value(raw_dict, "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = output_details.reasoning_tokens
    cached_input_tokens = _int_value(raw_dict, "cached_input_tokens")
    if cached_input_tokens is None:
        cached_input_tokens = input_details.cached_tokens
    usage = TokenUsage(
        provider_name=provider_name,
        model_id=model_id,
        input_tokens=_first_int_value(raw_dict, "input_tokens", "prompt_tokens"),
        output_tokens=_first_int_value(raw_dict, "output_tokens", "completion_tokens"),
        reasoning_tokens=reasoning_tokens,
        total_tokens=_int_value(raw_dict, "total_tokens"),
        cached_input_tokens=cached_input_tokens,
        input_details=input_details,
        output_details=output_details,
        raw=raw_dict,
    )
    if not any(
        value is not None
        for value in (
            usage.input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
            usage.total_tokens,
            usage.cached_input_tokens,
        )
    ):
        return None
    return usage


def _details(raw: object) -> TokenUsageDetails:
    if not isinstance(raw, Mapping):
        return TokenUsageDetails()
    raw_dict = dict(raw)
    return TokenUsageDetails(
        cached_tokens=_int_value(raw_dict, "cached_tokens"),
        reasoning_tokens=_int_value(raw_dict, "reasoning_tokens"),
        audio_tokens=_int_value(raw_dict, "audio_tokens"),
        accepted_prediction_tokens=_int_value(raw_dict, "accepted_prediction_tokens"),
        rejected_prediction_tokens=_int_value(raw_dict, "rejected_prediction_tokens"),
    )


def _mapping_value(raw: dict[object, object], key: str) -> object:
    value = raw.get(key)
    return value if isinstance(value, Mapping) else None


def _int_value(raw: dict[object, object], key: str) -> int | None:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _first_int_value(
    raw: dict[object, object],
    primary_key: str,
    fallback_key: str,
) -> int | None:
    primary = _int_value(raw, primary_key)
    return primary if primary is not None else _int_value(raw, fallback_key)
