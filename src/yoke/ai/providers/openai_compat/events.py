"""Provider telemetry helpers for OpenAI-compatible request retries."""

from __future__ import annotations

from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import ProviderRateLimitError
from yoke.ai.providers.base import emit_provider_event


def emit_retry_event(
    error: ProviderError,
    *,
    provider_name: str,
    model_id: str,
    attempt: int,
    max_retries: int | None,
    wait_seconds: float,
) -> None:
    """Emit one retry event with a compact, renderer-friendly payload."""
    payload: dict[str, object] = {
        "provider": provider_name,
        "model": model_id,
        "attempt": attempt + 1,
        "wait_seconds": round(wait_seconds, 1),
        "error_type": type(error).__name__,
        "message": str(error),
    }
    if max_retries is not None:
        payload["max_retries"] = max_retries
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        payload["status_code"] = status_code
    retry_after = getattr(error, "retry_after_seconds", None)
    if isinstance(retry_after, int | float):
        payload["retry_after_seconds"] = retry_after
    emit_provider_event(_retry_event_name(error), payload)


def emit_recovery_event(
    *,
    provider_name: str,
    model_id: str,
    attempts: int,
) -> None:
    """Emit an event after a request succeeds following one or more retries."""
    if attempts <= 0:
        return
    emit_provider_event(
        "provider_recovered",
        {
            "provider": provider_name,
            "model": model_id,
            "attempts": attempts,
        },
    )


def _retry_event_name(error: ProviderError) -> str:
    if isinstance(error, ProviderRateLimitError):
        return "provider_rate_limited"
    return "provider_retry"
