"""Retry behavior shared by OpenAI-compatible providers."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

import httpx

from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import ProviderRateLimitError
from yoke.ai.providers.base import ProviderServerError
from yoke.ai.providers.base import sleep_with_cancel
from yoke.ai.providers.openai_compat.events import emit_retry_event
from yoke.ai.providers.openai_compat.helpers import error_detail
from yoke.ai.providers.openai_compat.helpers import retry_after_seconds
from yoke.ai.providers.openai_compat.helpers import should_retry_request_error


class OpenAICompatibleRetryMixin:
    """Encapsulate retry classification, backoff, and cancellation."""

    config: Any
    provider_name: str
    _sleep: Callable[[float], None]

    def _backoff_seconds(self, attempt: int) -> float:
        return min(
            self.config.retry_backoff_seconds * (2**attempt),
            self.config.max_retry_backoff_seconds,
        )

    def _sleep_seconds(
        self, attempt: int, retry_after_seconds: float | None = None
    ) -> float:
        base_seconds = (
            self._backoff_seconds(attempt)
            if retry_after_seconds is None
            else retry_after_seconds
        )
        jitter = secrets.randbelow(1000) / 1000 * min(1.0, base_seconds * 0.1)
        return min(base_seconds + jitter, self.config.max_retry_backoff_seconds)

    def _retry_sleep(
        self,
        error: ProviderError,
        *,
        attempt: int,
        retry_after: float | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> float:
        wait_seconds = self._sleep_seconds(attempt, retry_after)
        emit_retry_event(
            error,
            provider_name=self.provider_name,
            model_id=self.config.model,
            attempt=attempt,
            max_retries=self.config.max_retries,
            wait_seconds=wait_seconds,
        )
        sleep_with_cancel(
            wait_seconds,
            cancel_requested=cancel_requested,
            sleep=self._sleep,
        )
        return wait_seconds

    def _handle_request_error(
        self,
        error: httpx.RequestError,
        *,
        attempt: int,
        cancel_requested: Callable[[], bool],
    ) -> ProviderError | None:
        if not should_retry_request_error(error):
            return None
        provider_error = ProviderError(f"Provider request failed: {error}")
        if attempt < self.config.max_retries:
            self._retry_sleep(
                provider_error,
                attempt=attempt,
                cancel_requested=cancel_requested,
            )
            return provider_error
        raise provider_error from error

    def _handle_error_response(
        self,
        response: httpx.Response,
        *,
        attempt: int,
        cancel_requested: Callable[[], bool],
    ) -> ProviderError | None:
        if response.status_code == 429:
            retry_after = retry_after_seconds(response)
            provider_error = ProviderRateLimitError(
                f"Provider request was rate limited: {error_detail(response)}",
                retry_after_seconds=retry_after,
            )
            if attempt < self.config.max_retries:
                self._retry_sleep(
                    provider_error,
                    attempt=attempt,
                    retry_after=retry_after,
                    cancel_requested=cancel_requested,
                )
                return provider_error
            raise provider_error
        if 500 <= response.status_code < 600:
            provider_error = ProviderServerError(
                f"Provider server error: {error_detail(response)}",
                status_code=response.status_code,
            )
            if attempt < self.config.max_retries:
                self._retry_sleep(
                    provider_error,
                    attempt=attempt,
                    cancel_requested=cancel_requested,
                )
                return provider_error
            raise provider_error
        if response.is_error:
            raise ProviderError(
                f"Provider request failed: {error_detail(response)}",
                status_code=response.status_code,
            )
        return None
