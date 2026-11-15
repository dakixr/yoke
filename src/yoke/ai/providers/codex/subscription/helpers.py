"""Codex reasoning normalization and OAuth error classification."""

from __future__ import annotations


def clamp_reasoning_effort(model: str, effort: str) -> str:
    normalized = effort.strip().lower()
    allowed = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    if normalized not in allowed:
        normalized = "medium"
    if model in {"gpt-6-astra", "gpt-6.1-sol"}:
        return (
            normalized
            if normalized in ("low", "medium", "high", "xhigh", "max")
            else "medium"
        )
    if model in {"gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna"}:
        return (
            normalized
            if normalized in ("none", "low", "medium", "high", "xhigh", "max")
            else "medium"
        )
    if normalized == "max" and not model.startswith("gpt-5.6"):
        return "xhigh" if "gpt-5" in model else "high"
    if normalized == "xhigh" and "gpt-5" not in model:
        return "high"
    return normalized


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def is_invalid_oauth_token_error(detail: object) -> bool:
    normalized = str(detail).strip().lower()
    return (
        "invalidated oauth token" in normalized
        or "invalid oauth token" in normalized
        or ("oauth token" in normalized and "invalid" in normalized)
        or ("oauth token" in normalized and "revoked" in normalized)
    )
