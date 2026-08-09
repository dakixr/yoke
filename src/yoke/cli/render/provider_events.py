"""Formatting helpers for provider retry and recovery telemetry."""

from __future__ import annotations

from yoke.cli.render.base import truncate_cli_text

PROVIDER_WARNING_STYLE = "#f0a030"


def is_provider_event(event: str) -> bool:
    """Return whether the runtime event is provider telemetry."""
    return event in {
        "provider_rate_limited",
        "provider_retry",
        "provider_recovered",
    }


def format_provider_event(
    event: str,
    payload: dict[str, object],
) -> str:
    """Format provider telemetry as a compact user-facing line."""
    provider = _text(payload.get("provider"), default="provider")
    model = _text(payload.get("model"), default="model")
    if event == "provider_recovered":
        attempts = payload.get("attempts")
        if isinstance(attempts, int) and attempts > 0:
            return f"{provider}:{model} recovered after {attempts} retry(s)."
        return f"{provider}:{model} recovered."
    retry_text = _retry_text(payload)
    wait_text = _wait_text(payload.get("wait_seconds"))
    message = truncate_cli_text(_text(payload.get("message")), 180)
    prefix = "rate limited" if event == "provider_rate_limited" else "retrying"
    if message:
        return f"{provider}:{model} {prefix}; {retry_text}; {wait_text}; {message}"
    return f"{provider}:{model} {prefix}; {retry_text}; {wait_text}"


def provider_status_for_event(event: str) -> str:
    """Return the toolbar/status label for provider telemetry."""
    if event == "provider_rate_limited":
        return "Rate limited"
    if event == "provider_recovered":
        return "Thinking"
    return "Retrying provider"


def _retry_text(payload: dict[str, object]) -> str:
    attempt = payload.get("attempt")
    max_retries = payload.get("max_retries")
    if isinstance(attempt, int) and isinstance(max_retries, int):
        return f"retry {attempt}/{max_retries}"
    if isinstance(attempt, int):
        return f"retry {attempt}"
    return "retry scheduled"


def _wait_text(value: object) -> str:
    if not isinstance(value, int | float):
        return "waiting"
    if value <= 0:
        return "retrying now"
    if value >= 60:
        minutes = value / 60
        return f"waiting {minutes:.1f} min"
    return f"waiting {value:.1f}s"


def _text(value: object, *, default: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default
