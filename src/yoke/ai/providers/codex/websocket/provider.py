"""Codex subscription provider over the Responses WebSocket transport."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from typing import cast

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    sleep_with_cancel,
)
from yoke.ai.providers.codex.response_state import CodexResponseChain
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionProvider,
    OAuthCredentials,
    exception_summary,
    is_invalid_oauth_token_error,
)
from yoke.ai.providers.codex.websocket.config import (
    PROVIDER_NAME,
    STALE_WEBSOCKET_CLOSED_MESSAGE,
    WEBSOCKET_REQUEST_TYPE,
    CodexConfig,
    CodexPreviousResponseNotFoundError,
    CodexWebSocketConnection,
    CodexWebSocketTimeoutError,
)
from yoke.ai.providers.codex.websocket.transport import CodexWebSocketTransportMixin


class CodexProvider(CodexWebSocketTransportMixin, CodexSubscriptionProvider):
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        config: CodexConfig,
        *,
        websocket_factory: Callable[..., CodexWebSocketConnection] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(config, sleep=sleep)
        self._websocket_factory = websocket_factory or cast(
            Callable[..., CodexWebSocketConnection],
            connect,
        )
        self._websocket: CodexWebSocketConnection | None = None
        self._websocket_credentials: OAuthCredentials | None = None
        self._websocket_auth_profile: str | None = None
        self._turn_state: str | None = None
        self._response_chain = CodexResponseChain()

    @property
    def config(self) -> CodexConfig:
        return self.__dict__["config"]

    @config.setter
    def config(self, value: CodexConfig) -> None:
        self.__dict__["config"] = value

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        return self.complete_with_cancel(
            messages,
            tools,
            cancel_requested=lambda: False,
        )

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        request_started = time.monotonic()
        request_log_id = secrets.token_hex(8)
        request_metrics = self._request_log_metrics(messages, tools)
        payload = self._request_payload(messages, tools)
        payload["type"] = WEBSOCKET_REQUEST_TYPE
        last_error: ProviderError | None = None

        for attempt in range(self.config.max_retries + 1):
            auth_profile = self._active_auth_profile
            try:
                websocket = self._fresh_websocket()
                auth_profile = self._websocket_auth_profile or self._active_auth_profile
                websocket_payload = self._prepare_websocket_payload(payload)
                continuity_mode = self._response_chain.prepared_mode
                try:
                    websocket.send(json.dumps(websocket_payload, separators=(",", ":")))
                except ConnectionClosed as exc:
                    self._close_websocket(clear_credentials=False)
                    raise ProviderError(STALE_WEBSOCKET_CLOSED_MESSAGE) from exc
                message = self._consume_websocket_response(
                    websocket,
                    cancel_requested=cancel_requested,
                )
                self._remember_successful_response(payload, message)
                usage = message.usage
                self._log_event(
                    "request_ok",
                    request_id=request_log_id,
                    attempt=attempt,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    auth_profile=self._active_auth_profile,
                    tool_call_count=len(message.tool_calls or []),
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                    total_tokens=getattr(usage, "total_tokens", None),
                    cached_input_tokens=getattr(usage, "cached_input_tokens", None),
                    prompt_cache_key_prefix=self._prompt_cache_key[:12],
                    used_previous_response_id="previous_response_id"
                    in websocket_payload,
                    continuity_mode=continuity_mode,
                    **request_metrics,
                )
                return message
            except ProviderRateLimitError as exc:
                last_error = exc
                self._close_websocket()
                if attempt >= self.config.max_retries:
                    break
                self._clear_selection_cache()
                self._log_event(
                    "request_retry",
                    request_id=request_log_id,
                    attempt=attempt,
                    reason="websocket_rate_limited",
                    wait_seconds=self._backoff_seconds(attempt),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                sleep_with_cancel(
                    self._backoff_seconds(attempt),
                    cancel_requested=cancel_requested,
                    sleep=self._sleep,
                )
            except ProviderServerError as exc:
                last_error = exc
                self._close_websocket()
                if attempt >= self.config.max_retries:
                    break
                self._log_event(
                    "request_retry",
                    request_id=request_log_id,
                    attempt=attempt,
                    reason="websocket_server_error",
                    wait_seconds=self._backoff_seconds(attempt),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                sleep_with_cancel(
                    self._backoff_seconds(attempt),
                    cancel_requested=cancel_requested,
                    sleep=self._sleep,
                )
            except ProviderCancelledError:
                self._close_websocket(clear_credentials=False)
                self._log_event(
                    "request_cancelled",
                    request_id=request_log_id,
                    attempt=attempt,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                raise
            except CodexWebSocketTimeoutError as exc:
                last_error = exc
                self._close_websocket(clear_credentials=False)
                if attempt >= self.config.max_retries:
                    break
                wait_seconds = self._backoff_seconds(attempt)
                self._log_event(
                    "request_retry",
                    request_id=request_log_id,
                    attempt=attempt,
                    reason="websocket_timeout",
                    wait_seconds=wait_seconds,
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                sleep_with_cancel(
                    wait_seconds,
                    cancel_requested=cancel_requested,
                    sleep=self._sleep,
                )
            except ProviderError as exc:
                last_error = exc
                if isinstance(exc, CodexPreviousResponseNotFoundError):
                    self._close_websocket(clear_credentials=False)
                    self._response_chain.drop_anchor()
                    if attempt >= self.config.max_retries:
                        break
                    self._log_event(
                        "request_retry",
                        request_id=request_log_id,
                        attempt=attempt,
                        reason="previous_response_not_found",
                        wait_seconds=0.0,
                        auth_profile=auth_profile,
                        **request_metrics,
                    )
                    continue
                if str(exc) == STALE_WEBSOCKET_CLOSED_MESSAGE:
                    self._close_websocket(clear_credentials=False)
                    if attempt >= self.config.max_retries:
                        break
                    self._log_event(
                        "request_retry",
                        request_id=request_log_id,
                        attempt=attempt,
                        reason="websocket_closed",
                        wait_seconds=self._backoff_seconds(attempt),
                        auth_profile=auth_profile,
                        **request_metrics,
                    )
                    sleep_with_cancel(
                        self._backoff_seconds(attempt),
                        cancel_requested=cancel_requested,
                        sleep=self._sleep,
                    )
                    continue
                self._close_websocket()
                if exc.status_code == 401 or is_invalid_oauth_token_error(str(exc)):
                    credentials = self._recover_invalid_oauth_credentials(
                        auth_profile=auth_profile,
                        request_id=request_log_id,
                        attempt=attempt,
                        detail=str(exc),
                        request_metrics=request_metrics,
                    )
                    if credentials is not None and attempt < self.config.max_retries:
                        self._websocket_credentials = credentials
                        continue
                self._log_request_failure(
                    request_log_id,
                    request_started,
                    attempt,
                    exc,
                    auth_profile,
                    request_metrics,
                )
                break
            except Exception as exc:
                self._close_websocket()
                last_error = ProviderError(f"Codex WebSocket request failed: {exc}")
                self._log_event(
                    "request_error",
                    request_id=request_log_id,
                    attempt=attempt,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    error=exception_summary(exc),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                if attempt >= self.config.max_retries:
                    break
                sleep_with_cancel(
                    self._backoff_seconds(attempt),
                    cancel_requested=cancel_requested,
                    sleep=self._sleep,
                )
        if last_error is not None:
            raise last_error
        raise ProviderError("Codex WebSocket request failed without a response.")
