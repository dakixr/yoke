"""Privacy-safe local provider usage metric logging."""

from __future__ import annotations

import os
import re
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai.providers.usage_context import (
    current_usage_metric_context,
)
from yoke.ai.providers.usage_writer import append_json_line
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
    path = usage_log_root() / _safe_directory_name(provider_name)
    path /= f"{completed_at.date().isoformat()}.jsonl"
    append_json_line(path, record)


def usage_log_root() -> Path:
    """Return the configured provider usage metric directory."""
    override = os.getenv("YOKE_USAGE_METRIC_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".yoke" / "usage-metric-logs"


def _provider_name(provider: object, usage: TokenUsage | None) -> str:
    if usage is not None and _text(usage.provider_name) is not None:
        return _text(usage.provider_name) or "unknown"
    return _text(getattr(provider, "provider_name", None)) or "unknown"


def _model_id(provider: object, usage: TokenUsage | None) -> str | None:
    if usage is not None and _text(usage.model_id) is not None:
        return _text(usage.model_id)
    current_model_id = getattr(provider, "current_model_id", None)
    if callable(current_model_id):
        return _text(current_model_id())
    return _text(getattr(getattr(provider, "config", None), "model", None))


def _usage_payload(usage: TokenUsage | None) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        field: value
        for field in _USAGE_FIELDS
        if isinstance((value := getattr(usage, field)), int)
    }


def _safe_directory_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-")
    return normalized or "unknown"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
