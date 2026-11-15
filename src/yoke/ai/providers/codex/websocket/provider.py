"""Codex subscription provider over the Responses WebSocket transport."""

from __future__ import annotations

import json
import platform
import secrets
import time
from collections.abc import Callable
from typing import Any, cast

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State
from websockets.sync.client import connect

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.codex.events import (
    CodexPreviousResponseNotFoundError,
    CodexResponseState,
    build_message,
    reduce_event,
)
from yoke.ai.providers.codex.response_state import CodexResponseChain
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionProvider,
    OAuthCredentials,
    exception_summary,
    is_invalid_oauth_token_error,
    originator_for_model,
    uses_responses_lite,
)
from yoke.ai.providers.codex.subscription.catalog import (
    list_provider_models as list_provider_models,
)
from yoke.ai.providers.codex.websocket.config import (
    PROVIDER_NAME,
    RESPONSES_LITE_CLIENT_METADATA_KEY,
    RESPONSES_WEBSOCKETS_BETA,
    STALE_WEBSOCKET_CLOSED_MESSAGE,
    WEBSOCKET_REQUEST_TYPE,
    WEBSOCKET_TIMEOUT_MESSAGE,
    X_CODEX_TURN_STATE_HEADER,
    CodexConfig,
    CodexWebSocketConnection,
    CodexWebSocketTimeoutError,
    ssl_context_for_websocket_url,
    websocket_url_for_base,
)
from yoke.ai.providers.http_retry import RetryDecision, run_with_retry

from .config import register_provider as register_provider


class CodexProvider(CodexSubscriptionProvider[CodexConfig]):
    _active_auth_profile: str | None
    _last_logged_auth_profile: str | None
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
        self._stream_started = False

    def fork_for_turn(self) -> CodexProvider:
        """Clone replayable response state without sharing a live WebSocket."""
        forked = type(self)(
            self.config.model_copy(deep=True),
            websocket_factory=self._websocket_factory,
            sleep=self._sleep,
        )
        forked._prompt_cache_key = self._prompt_cache_key
        forked._response_chain = self._response_chain.fork_for_new_connection()
        return forked

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
        auth_profile = self._active_auth_profile

        def attempt(number: int) -> Message:
            nonlocal auth_profile
            self._stream_started = False
            auth_profile = self._active_auth_profile
            websocket = self._fresh_websocket()
            auth_profile = self._websocket_auth_profile or self._active_auth_profile
            websocket_payload = self._prepare_websocket_payload(payload)
            continuity_mode = self._response_chain.prepared_mode
            continuity_reason = self._response_chain.prepared_reason
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
                attempt=number,
                duration_seconds=round(time.monotonic() - request_started, 3),
                auth_profile=self._active_auth_profile,
                tool_call_count=len(message.tool_calls or []),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                cached_input_tokens=usage.cached_input_tokens if usage else None,
                prompt_cache_key_prefix=self._prompt_cache_key[:12],
                used_previous_response_id="previous_response_id" in websocket_payload,
                continuity_mode=continuity_mode,
                continuity_reason=continuity_reason,
                **request_metrics,
            )
            return message

        def classify(error: Exception, number: int) -> RetryDecision:
            retry_after = (
                error.retry_after_seconds
                if isinstance(error, ProviderRateLimitError)
                else None
            )
            delay = (
                self._backoff_seconds(number, retry_after)
                if number < self.config.max_retries
                else 0.0
            )
            reason: str | None = None
            recover = False
            if isinstance(error, ProviderRateLimitError):
                self._close_websocket()
                reason = "websocket_rate_limited"
            elif isinstance(error, ProviderServerError):
                self._close_websocket()
                reason = "websocket_server_error"
            elif isinstance(error, ProviderCancelledError):
                self._close_websocket(clear_credentials=False)
                self._log_event(
                    "request_cancelled",
                    request_id=request_log_id,
                    attempt=number,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                return RetryDecision(error)
            elif isinstance(error, CodexWebSocketTimeoutError):
                self._close_websocket(clear_credentials=False)
                if self._stream_started:
                    return RetryDecision(
                        ProviderError(
                            "Codex WebSocket timed out after response bytes were received."
                        )
                    )
                reason = "websocket_timeout"
            elif isinstance(error, ProviderError):
                if isinstance(error, CodexPreviousResponseNotFoundError):
                    self._close_websocket(clear_credentials=False)
                    self._response_chain.drop_anchor()
                    reason, delay, recover = "previous_response_not_found", 0.0, True
                elif str(error) == STALE_WEBSOCKET_CLOSED_MESSAGE:
                    # A closed socket means the server dropped the generation.
                    # Replaying it is invisible (messages are assembled whole)
                    # and recovers from routine proxy idle closes, unlike a
                    # silent timeout where the server may still be generating.
                    self._close_websocket(clear_credentials=False)
                    reason = "websocket_closed"
                else:
                    self._close_websocket()
                    if error.status_code == 401 or is_invalid_oauth_token_error(
                        str(error)
                    ):
                        credentials = self._recover_invalid_oauth_credentials(
                            auth_profile=auth_profile,
                            request_id=request_log_id,
                            attempt=number,
                            detail=str(error),
                            request_metrics=request_metrics,
                        )
                        if credentials is not None and number < self.config.max_retries:
                            self._websocket_credentials = credentials
                            return RetryDecision(error, 0.0, recover=True)
                    self._log_request_failure(
                        request_log_id,
                        request_started,
                        number,
                        error,
                        auth_profile,
                        request_metrics,
                    )
                    return RetryDecision(error)
            else:
                self._close_websocket()
                mapped = ProviderError(f"Codex WebSocket request failed: {error}")
                self._log_event(
                    "request_error",
                    request_id=request_log_id,
                    attempt=number,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    error=exception_summary(error),
                    auth_profile=auth_profile,
                    **request_metrics,
                )
                # Unknown failures after a response frame must not replay work.
                return RetryDecision(mapped, None if self._stream_started else delay)

            def before_retry() -> None:
                if isinstance(error, ProviderRateLimitError):
                    self._clear_selection_cache()
                self._log_event(
                    "request_retry",
                    request_id=request_log_id,
                    attempt=number,
                    reason=reason,
                    wait_seconds=delay,
                    auth_profile=auth_profile,
                    **request_metrics,
                )

            return RetryDecision(
                error, delay, recover=recover, before_retry=before_retry
            )

        return run_with_retry(
            attempt,
            classify,
            max_retries=self.config.max_retries,
            cancel_requested=cancel_requested,
            sleep=self._sleep,
            check_cancel=False,
        )

    def close(self) -> None:
        self._close_websocket()
        self._response_chain.reset()
        CodexSubscriptionProvider.close(self)

    def set_session_id(self, session_id: str) -> None:
        """Switch cache affinity and response chaining to a new session."""
        previous_key = self._prompt_cache_key
        CodexSubscriptionProvider.set_session_id(self, session_id)
        if self._prompt_cache_key == previous_key:
            return
        self._close_websocket()
        self._response_chain.reset()

    def _request_payload(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> dict[str, object]:
        payload = CodexSubscriptionProvider._request_payload(self, messages, tools)
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict):
            typed_reasoning = cast(dict[str, object], reasoning)
            typed_reasoning["context"] = "all_turns"
        if not uses_responses_lite(self.config.model):
            return payload
        metadata = payload.get("client_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            payload["client_metadata"] = metadata
        typed_metadata = cast(dict[str, object], metadata)
        typed_metadata[RESPONSES_LITE_CLIENT_METADATA_KEY] = "true"
        return payload

    def _prepare_websocket_payload(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        credentials = self._websocket_credentials
        current_account_id = credentials.account_id if credentials is not None else None
        return self._response_chain.prepare(
            payload,
            account_id=current_account_id,
            auth_profile=self._websocket_auth_profile,
            selected_auth_profile=self._selected_auth_profile(),
        )

    def _remember_successful_response(
        self, payload: dict[str, object], message: Message
    ) -> None:
        credentials = self._websocket_credentials
        self._response_chain.remember(
            payload,
            message,
            account_id=credentials.account_id if credentials is not None else None,
            auth_profile=self._websocket_auth_profile,
        )

    def _request_headers(self, credentials: OAuthCredentials) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credentials.access}",
            "originator": originator_for_model(
                self.config.model, self.config.originator
            ),
            "User-Agent": (
                f"yoke ({platform.system().lower()}; {platform.machine().lower()})"
            ),
            "OpenAI-Beta": RESPONSES_WEBSOCKETS_BETA,
            "Content-Type": "application/json",
            # Codex backends and codex-lb derive cache and account affinity
            # from session_id, so a reconnect replay must reuse the cache key.
            "session_id": self._prompt_cache_key,
            "x-client-request-id": secrets.token_hex(16),
            **(
                {X_CODEX_TURN_STATE_HEADER: self._turn_state}
                if self._turn_state
                else {}
            ),
        }
        if not self.config.api_key:
            headers["chatgpt-account-id"] = credentials.account_id
        return headers

    def _fresh_credentials(self) -> OAuthCredentials:
        if self.config.api_key:
            self._active_auth_profile = "api-key"
            self._log_auth_profile_change("env", "api-key")
            return OAuthCredentials(
                access=self.config.api_key,
                refresh="api-key",
                expires=int((time.time() + 365 * 24 * 60 * 60) * 1000),
                account_id="api-key",
            )
        return CodexSubscriptionProvider._fresh_credentials(self)

    def _recover_invalid_oauth_credentials(
        self,
        *,
        auth_profile: str | None,
        request_id: str,
        attempt: int,
        detail: str,
        request_metrics: dict[str, object],
    ) -> OAuthCredentials | None:
        if self.config.api_key:
            self._log_event(
                "auth_invalidated",
                request_id=request_id,
                attempt=attempt,
                auth_profile=auth_profile,
                detail=detail,
                **request_metrics,
            )
            raise ProviderError("Codex API key was rejected. Check YOKE_CODEX_API_KEY.")
        return CodexSubscriptionProvider._recover_invalid_oauth_credentials(
            self,
            auth_profile=auth_profile,
            request_id=request_id,
            attempt=attempt,
            detail=detail,
            request_metrics=request_metrics,
        )

    def _responses_url(self) -> str:
        return websocket_url_for_base(self.config.base_url)

    def _request_log_metrics(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> dict[str, object]:
        metrics = CodexSubscriptionProvider._request_log_metrics(self, messages, tools)
        metrics["transport"] = "websocket"
        return metrics

    def _fresh_websocket(self) -> CodexWebSocketConnection:
        if self._websocket is not None and not self._websocket_closed(self._websocket):
            return self._websocket

        # Codex CLI probes cached sockets before reuse and reconnects when the
        # transport is already closed. Keep the sticky turn-state across that
        # reconnect, but do not keep using an expired bearer after a long tool
        # run made the previous socket stale.
        if self._websocket is not None:
            self._close_websocket(clear_credentials=False)
        credentials = self._valid_websocket_credentials() or self._fresh_credentials()
        self._websocket_credentials = credentials
        self._websocket_auth_profile = self._active_auth_profile
        try:
            websocket = self._websocket_factory(
                self._responses_url(),
                additional_headers=self._request_headers(credentials),
                open_timeout=self.config.timeout_seconds,
                close_timeout=min(self.config.timeout_seconds, 10),
                ping_interval=self.config.websocket_ping_interval_seconds,
                ping_timeout=self.config.websocket_ping_timeout_seconds,
                ssl=ssl_context_for_websocket_url(self._responses_url()),
            )
            self._websocket = websocket
            return websocket
        except InvalidStatus as exc:
            raise map_websocket_status_error(exc) from exc
        except Exception as exc:
            raise ProviderError(f"Codex WebSocket connection failed: {exc}") from exc

    def _websocket_closed(self, websocket: CodexWebSocketConnection) -> bool:
        try:
            if getattr(websocket, "closed", False):
                return True
            # websockets' sync client exposes connection state rather than
            # `closed`; its background reader records idle closes from proxies.
            return getattr(websocket, "state", None) in {State.CLOSING, State.CLOSED}
        except Exception:
            return False

    def _valid_websocket_credentials(
        self,
    ) -> OAuthCredentials | None:
        credentials = self._websocket_credentials
        if credentials is None:
            return None
        selected_auth_profile = self._selected_auth_profile()
        if self.config.accounts_dir.expanduser().exists() and (
            selected_auth_profile is None
            or self._websocket_auth_profile != selected_auth_profile
        ):
            return None
        if credentials.expires - int(time.time() * 1000) <= 60_000:
            return None
        return credentials

    def _selected_auth_profile(self) -> str | None:
        if not self.config.accounts_dir.expanduser().exists():
            return None
        try:
            payload = json.loads(
                self.config.selection_path.expanduser().read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        selected = payload.get("selected_profile")
        selected_at = payload.get("selected_at")
        if not isinstance(selected, str) or not isinstance(selected_at, int | float):
            return None
        if time.time() - float(selected_at) > self.config.selection_ttl_seconds:
            return None
        return selected

    def _consume_websocket_response(
        self,
        websocket: CodexWebSocketConnection,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Message:
        state = CodexResponseState(text_parts=[], function_calls={})
        self._response_chain.clear_staged_response()
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            if cancel_requested is not None and cancel_requested():
                raise ProviderCancelledError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexWebSocketTimeoutError(WEBSOCKET_TIMEOUT_MESSAGE)
            timeout = min(0.1, remaining)
            try:
                raw = websocket.recv(timeout=timeout)
            except TimeoutError as exc:
                if time.monotonic() < deadline:
                    continue
                raise CodexWebSocketTimeoutError(WEBSOCKET_TIMEOUT_MESSAGE) from exc
            except ConnectionClosed as exc:
                self._close_websocket(clear_credentials=False)
                raise ProviderError(STALE_WEBSOCKET_CLOSED_MESSAGE) from exc
            if raw:
                self._stream_started = True
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                continue
            # Match HTTP client timeout semantics: the timeout limits network
            # inactivity, not the total duration of an actively streaming response.
            deadline = time.monotonic() + self.config.timeout_seconds
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._capture_turn_state(event)
            reduce_event(event, state)
            if event.get("type") in {"response.completed", "response.done"}:
                message = build_message(
                    state,
                    provider_name=self.provider_name,
                    model_id=self.config.model,
                )
                self._response_chain.stage_response(
                    response_id=state.response_id,
                    output_items=state.output_items,
                )
                return message

    def _capture_turn_state(self, event: dict[str, Any]) -> None:
        # Codex CLI treats x-codex-turn-state as a server-provided sticky
        # routing token. It is replayed on later requests so reconnects still
        # reach the same warm backend, but it is intentionally separate from the
        # WebSocket object because a socket can be replaced after a stale close.
        if event.get("type") != "response.metadata":
            return
        headers = event.get("headers")
        if not isinstance(headers, dict):
            return
        for name, value in headers.items():
            if name.lower() != X_CODEX_TURN_STATE_HEADER:
                continue
            if isinstance(value, str) and value.strip():
                self._turn_state = value.strip()
            return

    def _close_websocket(self, *, clear_credentials: bool = True) -> None:
        websocket = self._websocket
        self._websocket = None
        self._response_chain.drop_anchor()
        if clear_credentials:
            self._websocket_credentials = None
            self._websocket_auth_profile = None
            self._turn_state = None
        if websocket is None:
            return
        try:
            websocket.close()
        except Exception:
            return


def map_websocket_status_error(exc: InvalidStatus) -> ProviderError:
    status_code = exc.response.status_code
    message = f"Codex WebSocket handshake failed: {exc}"
    if status_code == 429:
        return ProviderRateLimitError(message)
    if status_code in {500, 502, 503, 504}:
        return ProviderServerError(message, status_code=status_code)
    return ProviderError(
        message, status_code=status_code if isinstance(status_code, int) else None
    )
