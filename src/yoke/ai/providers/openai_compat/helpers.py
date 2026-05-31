"""Shared helpers for OpenAI-compatible providers."""

from __future__ import annotations

import httpx

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import cloned_model_catalog


def error_detail(response: httpx.Response) -> str:
    """Extract a readable error message from an HTTP response."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if isinstance(nested, str) and nested.strip():
                    return nested
    return response.text.strip() or f"HTTP {response.status_code}"


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a `retry-after` response header value when present."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def should_retry_request_error(error: httpx.RequestError) -> bool:
    """Return whether an `httpx` request error is likely transient."""
    return isinstance(
        error,
        httpx.ConnectError
        | httpx.ConnectTimeout
        | httpx.ReadError
        | httpx.ReadTimeout
        | httpx.RemoteProtocolError
        | httpx.WriteError
        | httpx.WriteTimeout,
    )


def build_model_catalog(
    *models: ProviderModelInfo,
) -> tuple[ProviderModelInfo, ...]:
    """Build a validated immutable provider model catalog."""
    return tuple(cloned_model_catalog(models))


def thinking_levels_for_reasoning_effort(
    reasoning_effort: str | None,
) -> tuple[str, ...]:
    """Map a configured reasoning effort to supported thinking levels."""
    if reasoning_effort is None:
        return (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
    normalized = reasoning_effort.strip().lower()
    return (
        (normalized,)
        if normalized
        else (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
    )
