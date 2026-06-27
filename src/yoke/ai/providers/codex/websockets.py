"""Codex subscription provider using the Responses WebSocket transport."""

# ruff: noqa: ANN401,D101,D102,D103,E501

from __future__ import annotations

import hashlib
import json
import platform
import secrets
import ssl
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from typing import Protocol
from typing import cast
from urllib.parse import urlparse
from urllib.parse import urlunparse

from pydantic import BaseModel
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from yoke.agent.models import Message
from yoke.agent.models import MessagePhase
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import ProviderRateLimitError
from yoke.ai.providers.base import ProviderServerError
from yoke.ai.providers.base import sleep_with_cancel
from yoke.ai.providers.codex.subscription import DEFAULT_BASE_URL
from yoke.ai.providers.codex.subscription import DEFAULT_CXAUTH_VAULT_NAME
from yoke.ai.providers.codex.subscription import DEFAULT_LOGS_DIR
from yoke.ai.providers.codex.subscription import DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
from yoke.ai.providers.codex.subscription import CodexSubscriptionConfig
from yoke.ai.providers.codex.subscription import CodexSubscriptionProvider
from yoke.ai.providers.codex.subscription import OAuthCredentials
from yoke.ai.providers.codex.subscription import clamp_reasoning_effort
from yoke.ai.providers.codex.subscription import convert_messages
from yoke.ai.providers.codex.subscription import convert_tools
from yoke.ai.providers.codex.subscription import default_reasoning_effort_for_model_id
from yoke.ai.providers.codex.subscription import exception_summary
from yoke.ai.providers.codex.subscription import is_invalid_oauth_token_error
from yoke.ai.providers.codex.subscription import list_provider_models
from yoke.ai.providers.codex.subscription import merge_completed_response
from yoke.ai.providers.codex.subscription import message_phase_from_completed_response
from yoke.ai.providers.codex.subscription import normalize_message_phase
from yoke.ai.providers.usage import parse_token_usage

PROVIDER_NAME = "codex-websockets"
RESPONSES_WEBSOCKETS_BETA = "responses_websockets=2026-02-06"
DEFAULT_WS_BASE_URL = DEFAULT_BASE_URL
STALE_WEBSOCKET_CLOSED_MESSAGE = "Codex WebSocket closed before response.completed."
WEBSOCKET_TIMEOUT_MESSAGE = "Codex WebSocket timed out waiting for response."
X_CODEX_TURN_STATE_HEADER = "x-codex-turn-state"
WEBSOCKET_REQUEST_TYPE = "response.create"


class CodexWebSocketTimeoutError(ProviderError):
    """Raised when an open WebSocket stops delivering response events."""


def register_provider(context: Any) -> CodexWebSockets:
    env = context.env or {}
    cxauth_vault = context.home / DEFAULT_CXAUTH_VAULT_NAME
    model = context.model or env.get("YOKE_CODEX_WEBSOCKETS_MODEL") or "gpt-5.5"
    return CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=context.home / ".codex" / "auth.json",
            accounts_dir=cxauth_vault / "accounts",
            auths_path=(
                Path(env.get("YOKE_CODEX_AUTHS_PATH", ""))
                if env.get("YOKE_CODEX_AUTHS_PATH")
                else context.home / ".yoke" / "providers" / "codex-auth" / "auths.json"
            ),
            selection_path=(
                Path(env.get("YOKE_CODEX_SELECTION_PATH", ""))
                if env.get("YOKE_CODEX_SELECTION_PATH")
                else context.home
                / ".yoke"
                / "providers"
                / "codex-auth"
                / "selection.json"
            ),
            selection_ttl_seconds=int(
                env.get("YOKE_CODEX_SELECTION_TTL_SECONDS") or "1800"
            ),
            model=model,
            base_url=(
                env.get("YOKE_CODEX_WEBSOCKETS_BASE_URL")
                or env.get("YOKE_CODEX_BASE_URL")
                or DEFAULT_WS_BASE_URL
            ),
            originator=env.get("YOKE_CODEX_ORIGINATOR") or "yoke",
            timeout_seconds=float(
                env.get("YOKE_CODEX_WEBSOCKETS_TIMEOUT_SECONDS")
                or env.get("YOKE_CODEX_TIMEOUT_SECONDS")
                or str(DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
            ),
            max_retries=int(
                env.get("YOKE_CODEX_WEBSOCKETS_MAX_RETRIES")
                or env.get("YOKE_CODEX_MAX_RETRIES")
                or "5"
            ),
            reasoning_effort=(
                context.reasoning_effort
                or env.get("YOKE_CODEX_WEBSOCKETS_REASONING_EFFORT")
                or env.get("YOKE_CODEX_REASONING_EFFORT")
                or default_reasoning_effort_for_model_id(model)
            ),
            text_verbosity=(
                env.get("YOKE_CODEX_WEBSOCKETS_TEXT_VERBOSITY")
                or env.get("YOKE_CODEX_TEXT_VERBOSITY")
                or "medium"
            ),
            logs_dir=Path(
                env.get("YOKE_CODEX_WEBSOCKETS_LOGS_DIR")
                or env.get("YOKE_CODEX_LOGS_DIR")
                or env.get("YOKE_PROVIDER_LOGS_DIR")
                or str(DEFAULT_LOGS_DIR)
            ),
            websocket_ping_interval_seconds=optional_float_env(
                env.get("YOKE_CODEX_WEBSOCKETS_PING_INTERVAL_SECONDS"),
                default=None,
            ),
            websocket_ping_timeout_seconds=optional_float_env(
                env.get("YOKE_CODEX_WEBSOCKETS_PING_TIMEOUT_SECONDS"),
                default=20.0,
            ),
        )
    )


class CodexWebSocketsConfig(CodexSubscriptionConfig):
    websocket_ping_interval_seconds: float | None = None
    websocket_ping_timeout_seconds: float | None = 20.0


def optional_float_env(value: str | None, *, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    if value.lower() in {"none", "off", "false", "0"}:
        return None
    return float(value)


class CodexWebSocketParseState(BaseModel):
    text_parts: list[str]
    snapshot_text_parts: list[str] = []
    function_calls: dict[str, dict[str, str]]
    completed_payload: dict[str, Any] | None = None
    response_id: str | None = None
    usage_payload: object | None = None
    phase: MessagePhase | None = None


class CodexWebSocketConnection(Protocol):
    def send(self, payload: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


class CodexWebSockets(CodexSubscriptionProvider):
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        config: CodexWebSocketsConfig,
        *,
        websocket_factory: Callable[..., CodexWebSocketConnection] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(config, sleep=sleep)
        self._websocket_factory = websocket_factory or connect
        self._websocket: CodexWebSocketConnection | None = None
        self._websocket_credentials: OAuthCredentials | None = None
        self._websocket_auth_profile: str | None = None
        self._prompt_cache_key = self._new_prompt_cache_key()
        self._turn_state: str | None = None
        self._last_request_payload: dict[str, object] | None = None
        self._last_response_id: str | None = None
        self._last_response_items: list[dict[str, Any]] = []
        self._last_response_account_id: str | None = None
        self._last_response_auth_profile: str | None = None
        self._pending_response_id: str | None = None

    @property
    def config(self) -> CodexWebSocketsConfig:
        return self.__dict__["config"]

    @config.setter
    def config(self, value: CodexWebSocketsConfig) -> None:
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

    def close(self) -> None:
        self._close_websocket()
        super().close()

    def _new_prompt_cache_key(self) -> str:
        seed = "\0".join(
            [
                str(self.config.auth_path.expanduser()),
                str(self.config.accounts_dir.expanduser()),
                self.config.base_url,
                self.config.model,
                secrets.token_hex(16),
            ]
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def _request_payload(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        instructions, input_items = convert_messages(messages)
        client_metadata: dict[str, object] = {}
        if self._turn_state:
            client_metadata[X_CODEX_TURN_STATE_HEADER] = self._turn_state
        payload: dict[str, object] = {
            "model": self.config.model,
            "store": False,
            "stream": True,
            "input": input_items,
            "text": {"verbosity": self.config.text_verbosity},
            "include": ["reasoning.encrypted_content"],
            # Match Codex CLI's cache strategy: keep a stable key for the CLI
            # session so server-side prompt caching survives reconnects. Socket
            # affinity alone is not the cache key, and randomizing this per
            # request defeats reuse after any reconnect.
            "prompt_cache_key": self._prompt_cache_key,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {
                "effort": clamp_reasoning_effort(
                    self.config.model, self.config.reasoning_effort
                ),
                "summary": "auto",
            },
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = convert_tools(tools)
        if client_metadata:
            payload["client_metadata"] = client_metadata
        return payload

    def _prepare_websocket_payload(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        websocket_payload = deepcopy(payload)
        previous_request = self._last_request_payload
        previous_response_id = self._last_response_id
        if previous_request is None or not previous_response_id:
            return websocket_payload
        credentials = self._websocket_credentials
        current_account_id = credentials.account_id if credentials is not None else None
        if (
            self._last_response_account_id != current_account_id
            or self._last_response_auth_profile != self._websocket_auth_profile
        ):
            return websocket_payload
        if not response_request_properties_match(previous_request, payload):
            return websocket_payload
        previous_input = previous_request.get("input")
        current_input = payload.get("input")
        if not isinstance(previous_input, list) or not isinstance(current_input, list):
            return websocket_payload
        after_previous_input = strip_list_prefix(current_input, previous_input)
        if after_previous_input is None:
            return websocket_payload
        incremental_items = strip_list_prefix(
            after_previous_input,
            self._last_response_items,
        )
        if incremental_items is None:
            return websocket_payload
        websocket_payload["previous_response_id"] = previous_response_id
        websocket_payload["input"] = incremental_items
        return websocket_payload

    def _remember_successful_response(
        self, payload: dict[str, object], message: Message
    ) -> None:
        response_id = self._pending_response_id
        self._pending_response_id = None
        if not response_id:
            self._reset_response_link()
            return
        self._last_request_payload = deepcopy(payload)
        _, response_items = convert_messages([message])
        self._last_response_id = response_id
        self._last_response_items = response_items
        credentials = self._websocket_credentials
        self._last_response_account_id = (
            credentials.account_id if credentials is not None else None
        )
        self._last_response_auth_profile = self._websocket_auth_profile

    def _reset_response_link(self) -> None:
        self._last_request_payload = None
        self._last_response_id = None
        self._last_response_items = []
        self._last_response_account_id = None
        self._last_response_auth_profile = None
        self._pending_response_id = None

    def _request_headers(self, credentials: OAuthCredentials) -> dict[str, str]:
        request_id = secrets.token_hex(16)
        return {
            "Authorization": f"Bearer {credentials.access}",
            "chatgpt-account-id": credentials.account_id,
            "originator": self.config.originator,
            "User-Agent": (
                f"yoke ({platform.system().lower()}; {platform.machine().lower()})"
            ),
            "OpenAI-Beta": RESPONSES_WEBSOCKETS_BETA,
            "Content-Type": "application/json",
            "session_id": request_id,
            "x-client-request-id": request_id,
            **(
                {X_CODEX_TURN_STATE_HEADER: self._turn_state}
                if self._turn_state
                else {}
            ),
        }

    def _responses_url(self) -> str:
        return websocket_url_for_base(self.config.base_url)

    def _request_log_metrics(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        metrics = super()._request_log_metrics(messages, tools)
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
            websocket = cast(
                CodexWebSocketConnection,
                self._websocket_factory(
                    self._responses_url(),
                    additional_headers=self._request_headers(credentials),
                    open_timeout=self.config.timeout_seconds,
                    close_timeout=min(self.config.timeout_seconds, 10),
                    ping_interval=self.config.websocket_ping_interval_seconds,
                    ping_timeout=self.config.websocket_ping_timeout_seconds,
                    ssl=ssl_context_for_websocket_url(self._responses_url()),
                ),
            )
            self._websocket = websocket
            return websocket
        except InvalidStatus as exc:
            raise map_websocket_status_error(exc) from exc
        except Exception as exc:
            raise ProviderError(f"Codex WebSocket connection failed: {exc}") from exc

    def _websocket_closed(self, websocket: CodexWebSocketConnection) -> bool:
        try:
            return bool(getattr(websocket, "closed", False))
        except Exception:
            return False

    def _valid_websocket_credentials(self) -> OAuthCredentials | None:
        credentials = self._websocket_credentials
        if credentials is None:
            return None
        if credentials.expires - int(time.time() * 1000) <= 60_000:
            return None
        return credentials

    def _consume_websocket_response(
        self,
        websocket: CodexWebSocketConnection,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Message:
        state = CodexWebSocketParseState(text_parts=[], function_calls={})
        self._pending_response_id = None
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
            handle_websocket_event(event, state)
            if event.get("type") in {"response.completed", "response.done"}:
                message = build_message_from_websocket_state(
                    state,
                    provider_name=self.provider_name,
                    model_id=self.config.model,
                )
                self._pending_response_id = state.response_id
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
        if clear_credentials:
            self._websocket_credentials = None
            self._websocket_auth_profile = None
            self._turn_state = None
            self._reset_response_link()
        if websocket is None:
            return
        try:
            websocket.close()
        except Exception:
            return


def websocket_url_for_base(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        response_path = path
    elif path.endswith("/codex"):
        response_path = f"{path}/responses"
    elif path.endswith("/v1"):
        response_path = f"{path}/responses"
    else:
        response_path = f"{path}/codex/responses"
    return urlunparse(
        (
            scheme,
            parsed.netloc,
            response_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def ssl_context_for_websocket_url(url: str) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    context = ssl.create_default_context()
    return context


def response_request_properties_match(
    previous: dict[str, object], current: dict[str, object]
) -> bool:
    ignored_keys = {"input", "client_metadata", "previous_response_id"}
    previous_properties = {
        key: value for key, value in previous.items() if key not in ignored_keys
    }
    current_properties = {
        key: value for key, value in current.items() if key not in ignored_keys
    }
    return previous_properties == current_properties


def strip_list_prefix(items: list[Any], prefix: list[Any]) -> list[Any] | None:
    if len(prefix) > len(items):
        return None
    if items[: len(prefix)] != prefix:
        return None
    return items[len(prefix) :]


def handle_websocket_event(
    event: dict[str, Any], state: CodexWebSocketParseState
) -> None:
    event_type = event.get("type")
    if isinstance(event.get("usage"), dict):
        state.usage_payload = event.get("usage")
    if event_type == "response.created":
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            response_id = response_payload.get("id")
            if isinstance(response_id, str) and response_id:
                state.response_id = response_id
    if event_type in {"error", "response.failed"}:
        raise map_websocket_error_event(event)
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str):
            state.text_parts.append(delta)
    elif event_type == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or event.get("output_index") or "")
        if item_id:
            item = state.function_calls.setdefault(item_id, {})
            item["arguments"] = item.get("arguments", "") + str(
                event.get("delta") or ""
            )
    elif event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict):
            handle_websocket_output_item(item, state)
    elif event_type in {"response.completed", "response.done"}:
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            state.completed_payload = response_payload
            response_id = response_payload.get("id")
            if isinstance(response_id, str) and response_id:
                state.response_id = response_id
            state.usage_payload = response_payload.get("usage") or state.usage_payload


def handle_websocket_output_item(
    item: dict[str, Any], state: CodexWebSocketParseState
) -> None:
    if item.get("type") == "function_call":
        item_id = str(
            item.get("id") or item.get("call_id") or len(state.function_calls)
        )
        stored = state.function_calls.setdefault(item_id, {})
        stored["call_id"] = str(item.get("call_id") or item_id)
        stored["name"] = str(item.get("name") or "")
        stored["arguments"] = str(
            item.get("arguments") or stored.get("arguments") or "{}"
        )
        return
    if item.get("type") != "message":
        return
    phase = normalize_message_phase(item.get("phase"))
    if phase == "final_answer" or (phase == "commentary" and state.phase is None):
        state.phase = phase
    for content in item.get("content") or []:
        if not isinstance(content, dict):
            continue
        if content.get("type") in {"output_text", "text"}:
            text = content.get("text")
            if isinstance(text, str):
                state.snapshot_text_parts.append(text)


def build_message_from_websocket_state(
    state: CodexWebSocketParseState,
    *,
    provider_name: str,
    model_id: str,
) -> Message:
    if state.completed_payload is not None:
        merge_completed_response(
            state.completed_payload,
            state.text_parts if state.text_parts else state.snapshot_text_parts,
            state.function_calls,
        )
        state.usage_payload = (
            state.completed_payload.get("usage") or state.usage_payload
        )
    phase = (
        message_phase_from_completed_response(state.completed_payload) or state.phase
    )
    text_parts = state.text_parts or state.snapshot_text_parts
    tool_calls = [
        ToolCall(
            id=item.get("call_id") or item_id,
            function=ToolFunction(
                name=item.get("name") or "",
                arguments=item.get("arguments") or "{}",
            ),
        )
        for item_id, item in state.function_calls.items()
        if item.get("name")
    ]
    return Message(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        phase=phase,
        usage=parse_token_usage(
            state.usage_payload,
            provider_name=provider_name,
            model_id=model_id,
        ),
    )


def map_websocket_error_event(event: dict[str, Any]) -> ProviderError:
    error_payload = event.get("error") if isinstance(event, dict) else None
    error_type = ""
    error_code = ""
    error_message = ""
    if isinstance(error_payload, dict):
        error_type = str(error_payload.get("type") or "").lower()
        error_code = str(error_payload.get("code") or "").lower()
        error_message = str(error_payload.get("message") or "").lower()
    status_code = event.get("status") or event.get("status_code")
    haystack = f"{error_type} {error_code} {error_message}"
    if "websocket_connection_limit_reached" in haystack:
        return ProviderServerError(
            f"Codex WebSocket connection limit reached: {event}",
            status_code=503,
        )
    if "rate_limit" in haystack or status_code == 429:
        return ProviderRateLimitError(f"Codex WebSocket rate limited: {event}")
    if status_code in {500, 502, 503, 504} or any(
        marker in haystack
        for marker in (
            "server_error",
            "service_unavailable",
            "internal_error",
            "overloaded",
            "timeout",
            "bad_gateway",
            "gateway_timeout",
        )
    ):
        return ProviderServerError(
            f"Codex WebSocket stream failed: {event}",
            status_code=503,
        )
    return ProviderError(
        f"Codex WebSocket stream failed: {event}",
        status_code=status_code if isinstance(status_code, int) else None,
    )


def map_websocket_status_error(exc: InvalidStatus) -> ProviderError:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(
        response, "status", None
    )
    message = f"Codex WebSocket handshake failed: {exc}"
    if status_code == 429:
        return ProviderRateLimitError(message)
    if status_code in {500, 502, 503, 504}:
        return ProviderServerError(message, status_code=status_code)
    return ProviderError(
        message, status_code=status_code if isinstance(status_code, int) else None
    )


__all__ = [
    "CodexWebSockets",
    "CodexWebSocketsConfig",
    "PROVIDER_NAME",
    "X_CODEX_TURN_STATE_HEADER",
    "list_provider_models",
    "register_provider",
    "websocket_url_for_base",
]
