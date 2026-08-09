"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import time
from typing import Any

import httpx

from yoke.ai.providers.base import (
    ProviderError,
)

from .catalog import DEFAULT_USAGE_URL
from .helpers import error_detail
from .models import OAuthCredentials
from .oauth import refresh_openai_codex_token
from .profiles import (
    AccountScore,
    CodexProfile,
    Pace,
    QuotaLimit,
    QuotaSnapshot,
    QuotaWindow,
)


def query_codex_quota(auth_data: dict[str, Any]) -> QuotaSnapshot:
    credentials = _credentials_from_codex_auth_payload(auth_data)
    updated_auth = auth_data
    if credentials.expires - int(time.time() * 1000) <= 60_000:
        credentials = refresh_openai_codex_token(credentials)
        updated_auth = _codex_auth_payload_with_credentials(auth_data, credentials)
    usage = _fetch_codex_oauth_usage(credentials)
    return QuotaSnapshot(
        default_limit=_parse_oauth_usage_limit(usage), updated_auth=updated_auth
    )


def score_quota_snapshot(snapshot: QuotaSnapshot) -> AccountScore:
    limit = snapshot.default_limit
    session = limit.primary if limit else None
    weekly = limit.secondary if limit else None
    session_used = session.used_percent if session else None
    weekly_used = weekly.used_percent if weekly else None
    session_pace = _pace_for_window(session, default_window_minutes=300)
    weekly_pace = _pace_for_window(weekly, default_window_minutes=10080)
    if weekly_used is not None and weekly_used >= 98:
        return AccountScore(score=float("inf"), rejected=True)
    if session_used is not None and session_used >= 98:
        if session_pace is None or session_pace.resets_in_seconds > 10 * 60:
            return AccountScore(score=float("inf"), rejected=True)
    score = 0.0
    score += float(session_used if session_used is not None else 999)
    score += float(weekly_used if weekly_used is not None else 999) * 2
    score += _pace_pressure(session_pace, deficit_weight=1.5, reserve_weight=0.5)
    score += _pace_pressure(weekly_pace, deficit_weight=3.0, reserve_weight=1.0)
    return AccountScore(score=score, rejected=False)


def _pace_for_window(
    window: QuotaWindow | None, *, default_window_minutes: int
) -> Pace | None:
    if window is None or window.used_percent is None or window.resets_at is None:
        return None
    window_minutes = window.duration_mins or default_window_minutes
    if window_minutes <= 0:
        return None
    duration = float(window_minutes * 60)
    resets_in = float(window.resets_at) - time.time()
    if resets_in <= 0 or resets_in > duration:
        return None
    elapsed = max(0.0, min(duration, duration - resets_in))
    actual = max(0.0, min(float(window.used_percent), 100.0))
    expected = max(0.0, min((elapsed / duration) * 100.0, 100.0))
    return Pace(delta_percent=actual - expected, resets_in_seconds=resets_in)


def _pace_pressure(
    pace: Pace | None, *, deficit_weight: float, reserve_weight: float
) -> float:
    if pace is None:
        return 0.0
    if pace.delta_percent > 0:
        return pace.delta_percent * deficit_weight
    return pace.delta_percent * reserve_weight


def _credentials_from_codex_auth_payload(
    auth_data: dict[str, Any],
) -> OAuthCredentials:
    return CodexProfile("quota-probe", auth_data).credentials()


def _codex_auth_payload_with_credentials(
    auth_data: dict[str, Any], credentials: OAuthCredentials
) -> dict[str, Any]:
    return CodexProfile("quota-probe", auth_data).with_credentials(credentials)


def _fetch_codex_oauth_usage(credentials: OAuthCredentials) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {credentials.access}",
        "ChatGPT-Account-Id": credentials.account_id,
        "User-Agent": "yoke",
        "Accept": "application/json",
    }
    try:
        response = httpx.get(
            DEFAULT_USAGE_URL,
            headers=headers,
            timeout=30,
        )
    except httpx.RequestError as exc:
        raise ProviderError(f"Codex OAuth usage request failed: {exc}") from exc
    if response.is_error:
        raise ProviderError(
            f"Codex OAuth usage request failed: {error_detail(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            "Codex OAuth usage endpoint returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError("Codex OAuth usage endpoint returned invalid payload.")
    return payload


def _parse_oauth_usage_limit(payload: dict[str, Any]) -> QuotaLimit | None:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    return _normalize_quota_limit(
        QuotaLimit(
            primary=_parse_oauth_usage_window(rate_limit.get("primary_window")),
            secondary=_parse_oauth_usage_window(rate_limit.get("secondary_window")),
        )
    )


def _parse_oauth_usage_window(raw: Any) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    resets = raw.get("reset_at")
    duration_seconds = raw.get("limit_window_seconds")
    return QuotaWindow(
        used_percent=used if isinstance(used, int) else None,
        resets_at=resets if isinstance(resets, int) else None,
        duration_mins=(
            duration_seconds // 60 if isinstance(duration_seconds, int) else None
        ),
    )


def _normalize_quota_limit(limit: QuotaLimit) -> QuotaLimit | None:
    primary = limit.primary
    secondary = limit.secondary
    if primary is None and secondary is None:
        return None
    primary_role = _quota_window_role(primary)
    secondary_role = _quota_window_role(secondary)
    if primary is not None and secondary is not None:
        if primary_role == "weekly" and secondary_role in {
            "session",
            "unknown",
        }:
            return QuotaLimit(primary=secondary, secondary=primary)
        return limit
    if primary is not None and primary_role == "weekly":
        return QuotaLimit(primary=None, secondary=primary)
    if secondary is not None and secondary_role in {"session", "unknown"}:
        return QuotaLimit(primary=secondary, secondary=None)
    return limit


def _quota_window_role(window: QuotaWindow | None) -> str:
    if window is None:
        return "none"
    if window.duration_mins == 300:
        return "session"
    if window.duration_mins == 10080:
        return "weekly"
    return "unknown"
