"""Privacy-safe local provider usage metric logging."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

from yoke.ai.providers.base import provider_identity
from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.agent_runs.reporting import report_provider_usage
from yoke.ai.providers.usage_context import (
    current_usage_metric_context,
)
from yoke.ai.providers.usage_writer import append_usage
from yoke.ai.providers.usage_writer import UsageLogWriteError as UsageLogWriteError

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def record_provider_usage(provider: object, response: Message) -> None:
    """Append one durable metric for a completed provider response."""
    completed_at = datetime.now(UTC)
    usage = response.usage
    report_provider_usage(usage)
    provider_name = _provider_name(provider, usage)
    record: dict[str, object] = {
        "schema_version": 1,
        "event_id": uuid4().hex,
        "completed_at": completed_at.isoformat(),
        **current_usage_metric_context().record_fields(),
        "provider": provider_name,
        "model": _model_id(provider, usage),
        "usage_reported": usage is not None,
        "usage": _usage_payload(usage),
    }
    append_usage(record)


def _provider_name(provider: object, usage: TokenUsage | None) -> str:
    if usage is not None and _text(usage.provider_name) is not None:
        return _text(usage.provider_name) or "unknown"
    return provider_identity(provider).name


def _model_id(provider: object, usage: TokenUsage | None) -> str | None:
    if usage is not None and _text(usage.model_id) is not None:
        return _text(usage.model_id)
    return provider_identity(provider).model


def _usage_payload(usage: TokenUsage | None) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        field: value
        for field in _USAGE_FIELDS
        if isinstance((value := getattr(usage, field)), int)
    }


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
