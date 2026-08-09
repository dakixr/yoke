"""Retry timing and HTTP error parsing for Z.AI."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from yoke.ai.providers.zai.models import ZAIConfig


class ZAIRetryMixin:
    config: ZAIConfig

    def _backoff_seconds(self, attempt: int) -> float:
        delay = self.config.retry_backoff_seconds * (2**attempt)
        return min(delay, self.config.max_retry_backoff_seconds)

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        return max(delay, 0.0)

    def _error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if (
                isinstance(error, dict)
                and isinstance(error.get("message"), str)
                and error["message"].strip()
            ):
                return error["message"].strip()
            if isinstance(error, str) and error.strip():
                return error.strip()
        return response.reason_phrase or f"HTTP {response.status_code}"
