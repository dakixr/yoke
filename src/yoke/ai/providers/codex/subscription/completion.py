"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from typing import Any

import secrets
import threading
import time
from collections.abc import Callable

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    sleep_with_cancel,
)
from yoke.ai.providers.codex.cache import build_prompt_cache_key

from .helpers import error_detail, is_invalid_oauth_token_error, retry_after_seconds
from .sse import consume_sse_response


class CodexCompletionMixin:
    def _complete_with_cancel_impl(
        self: Any,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        request_started = time.monotonic()
        request_log_id = secrets.token_hex(8)
        request_metrics = self._request_log_metrics(messages, tools)
        credentials = self._fresh_credentials()
        auth_profile = self._active_auth_profile
        payload = self._request_payload(messages, tools)
        headers = self._request_headers(credentials)
        last_error: ProviderError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with self._client.stream(
                    "POST",
                    self._responses_url(),
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code == 429:
                        retry_after = retry_after_seconds(response)
                        last_error = ProviderRateLimitError(
                            f"Codex request was rate limited: {error_detail(response)}",
                            retry_after_seconds=retry_after,
                        )
                        if attempt < self.config.max_retries:
                            wait_seconds = retry_after or self._backoff_seconds(attempt)
                            self._clear_selection_cache()
                            credentials = self._fresh_credentials()
                            auth_profile = self._active_auth_profile
                            headers = self._request_headers(credentials)
                            self._log_event(
                                "request_retry",
                                request_id=request_log_id,
                                attempt=attempt,
                                status_code=response.status_code,
                                wait_seconds=wait_seconds,
                                retry_after_used=retry_after is not None,
                                reason="rate_limited",
                                auth_profile=auth_profile,
                                **request_metrics,
                            )
                            sleep_with_cancel(
                                wait_seconds,
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error
                    if 500 <= response.status_code < 600:
                        last_error = ProviderServerError(
                            f"Codex server error: {error_detail(response)}",
                            status_code=response.status_code,
                        )
                        if attempt < self.config.max_retries:
                            wait_seconds = self._backoff_seconds(attempt)
                            self._log_event(
                                "request_retry",
                                request_id=request_log_id,
                                attempt=attempt,
                                status_code=response.status_code,
                                wait_seconds=wait_seconds,
                                retry_after_used=False,
                                reason="server_error",
                                auth_profile=auth_profile,
                                **request_metrics,
                            )
                            sleep_with_cancel(
                                wait_seconds,
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error
                    if response.is_error:
                        detail = error_detail(response)
                        if is_invalid_oauth_token_error(detail):
                            recovered = self._recover_invalid_oauth_credentials(
                                auth_profile=auth_profile,
                                request_id=request_log_id,
                                attempt=attempt,
                                detail=detail,
                                request_metrics=request_metrics,
                            )
                            if (
                                recovered is not None
                                and attempt < self.config.max_retries
                            ):
                                credentials = recovered
                                auth_profile = self._active_auth_profile
                                headers = self._request_headers(credentials)
                                self._log_event(
                                    "request_retry",
                                    request_id=request_log_id,
                                    attempt=attempt,
                                    status_code=response.status_code,
                                    wait_seconds=0.0,
                                    retry_after_used=False,
                                    reason="invalid_oauth_token",
                                    auth_profile=auth_profile,
                                    **request_metrics,
                                )
                                continue
                        raise ProviderError(
                            f"Codex request failed: {detail}",
                            status_code=response.status_code,
                        )
                    message = consume_sse_response(
                        response,
                        provider_name=self.provider_name,
                        model_id=self.config.model,
                        cancel_requested=cancel_requested,
                        turn_state_updated=self._set_turn_state,
                    )
                    usage = message.usage
                    self._log_event(
                        "request_success",
                        request_id=request_log_id,
                        attempt=attempt,
                        duration_seconds=round(time.monotonic() - request_started, 3),
                        tool_call_count=len(message.tool_calls or []),
                        input_tokens=getattr(usage, "input_tokens", None),
                        output_tokens=getattr(usage, "output_tokens", None),
                        total_tokens=getattr(usage, "total_tokens", None),
                        auth_profile=auth_profile,
                        **request_metrics,
                    )
                    return message
            except ProviderRateLimitError as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    wait_seconds = exc.retry_after_seconds or self._backoff_seconds(
                        attempt
                    )
                    self._clear_selection_cache()
                    credentials = self._fresh_credentials()
                    auth_profile = self._active_auth_profile
                    headers = self._request_headers(credentials)
                    self._log_event(
                        "request_retry",
                        request_id=request_log_id,
                        attempt=attempt,
                        wait_seconds=wait_seconds,
                        retry_after_used=exc.retry_after_seconds is not None,
                        reason="stream_rate_limited",
                        auth_profile=auth_profile,
                        **request_metrics,
                    )
                    sleep_with_cancel(
                        wait_seconds,
                        cancel_requested=cancel_requested,
                        sleep=self._sleep,
                    )
                    continue
                self._log_request_failure(
                    request_log_id,
                    request_started,
                    attempt,
                    exc,
                    auth_profile,
                    request_metrics,
                )
                raise
            except ProviderServerError as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    wait_seconds = self._backoff_seconds(attempt)
                    self._log_event(
                        "request_retry",
                        request_id=request_log_id,
                        attempt=attempt,
                        status_code=exc.status_code,
                        wait_seconds=wait_seconds,
                        retry_after_used=False,
                        reason="stream_server_error",
                        auth_profile=auth_profile,
                        **request_metrics,
                    )
                    sleep_with_cancel(
                        wait_seconds,
                        cancel_requested=cancel_requested,
                        sleep=self._sleep,
                    )
                    continue
                self._log_request_failure(
                    request_log_id,
                    request_started,
                    attempt,
                    exc,
                    auth_profile,
                    request_metrics,
                )
                raise
            except httpx.TimeoutException as exc:
                last_error = ProviderError("Codex request timed out.")
                if attempt < self.config.max_retries:
                    wait_seconds = self._backoff_seconds(attempt)
                    self._log_event(
                        "request_retry",
                        request_id=request_log_id,
                        attempt=attempt,
                        wait_seconds=wait_seconds,
                        retry_after_used=False,
                        reason="timeout",
                        auth_profile=auth_profile,
                        **request_metrics,
                    )
                    sleep_with_cancel(
                        wait_seconds,
                        cancel_requested=cancel_requested,
                        sleep=self._sleep,
                    )
                    continue
                self._log_request_failure(
                    request_log_id,
                    request_started,
                    attempt,
                    last_error,
                    auth_profile,
                    request_metrics,
                )
                raise last_error from exc
            except ProviderCancelledError:
                self._log_event(
                    "request_cancelled",
                    request_id=request_log_id,
                    attempt=attempt,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                raise
            except httpx.RequestError as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                last_error = ProviderError(f"Codex request failed: {exc}")
                self._log_request_failure(
                    request_log_id,
                    request_started,
                    attempt,
                    last_error,
                    auth_profile,
                    request_metrics,
                )
                raise last_error from exc
        if last_error is not None:
            self._log_request_failure(
                request_log_id,
                request_started,
                self.config.max_retries,
                last_error,
                auth_profile,
                request_metrics,
            )
            raise last_error
        raise ProviderError("Codex request failed unexpectedly.")

    def _with_request_cancellation(
        self: Any,
        action: Callable[[], Message],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        if not self._owns_client:
            return action()
        finished = threading.Event()
        client_closed = threading.Event()

        def close_on_cancel() -> None:
            while not finished.wait(0.05):
                if cancel_requested():
                    client_closed.set()
                    self._client.close()
                    return

        threading.Thread(target=close_on_cancel, daemon=True).start()
        try:
            message = action()
            if cancel_requested():
                raise ProviderCancelledError()
            return message
        finally:
            finished.set()
            if client_closed.is_set():
                self._client = self._new_client()

    def close(self: Any) -> None:
        if self._owns_client:
            self._client.close()

    def _set_turn_state(self: Any, value: str) -> None:
        self._turn_state = value

    def start_turn(self: Any) -> None:
        """Clear Codex routing metadata at a logical user-turn boundary."""
        self._turn_state = None

    def _new_prompt_cache_key(self: Any) -> str:
        return build_prompt_cache_key(self.config)

    def set_session_id(self: Any, session_id: str) -> None:
        self.config.prompt_cache_key = session_id
        self._prompt_cache_key = self._new_prompt_cache_key()
        self._turn_state = None
