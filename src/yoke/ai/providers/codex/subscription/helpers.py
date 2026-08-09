"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations


import httpx


def clamp_reasoning_effort(model: str, effort: str) -> str:
    normalized = effort.strip().lower()
    allowed = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    if normalized not in allowed:
        normalized = "medium"
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


def error_detail(response: httpx.Response) -> str:
    try:
        response.read()
    except httpx.ResponseNotRead:
        pass
    except httpx.CloseError:
        return f"HTTP {response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return response.text.strip() or f"HTTP {response.status_code}"


def is_invalid_oauth_token_error(detail: object) -> bool:
    normalized = str(detail).strip().lower()
    return (
        "invalidated oauth token" in normalized
        or "invalid oauth token" in normalized
        or ("oauth token" in normalized and "invalid" in normalized)
        or ("oauth token" in normalized and "revoked" in normalized)
    )


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
