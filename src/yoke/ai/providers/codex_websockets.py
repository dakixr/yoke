"""Codex subscription provider using the Responses WebSocket transport."""

# ruff: noqa: ANN401,D101,D102,D103,E501

from __future__ import annotations

import json
import platform
import secrets
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.parse import urlunparse

from pydantic import BaseModel
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import InvalidStatus
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect

from yoke.agent.models import Message
from yoke.agent.models import MessagePhase
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import ProviderRateLimitError
from yoke.ai.providers.base import ProviderServerError
from yoke.ai.providers.codex_subscription import DEFAULT_BASE_URL
from yoke.ai.providers.codex_subscription import DEFAULT_CXAUTH_VAULT_NAME
from yoke.ai.providers.codex_subscription import DEFAULT_LOGS_DIR
from yoke.ai.providers.codex_subscription import CodexSubscriptionConfig
from yoke.ai.providers.codex_subscription import CodexSubscriptionProvider
from yoke.ai.providers.codex_subscription import OAuthCredentials
from yoke.ai.providers.codex_subscription import clamp_reasoning_effort
from yoke.ai.providers.codex_subscription import convert_messages
from yoke.ai.providers.codex_subscription import convert_tools
from yoke.ai.providers.codex_subscription import count_message_images
from yoke.ai.providers.codex_subscription import default_reasoning_effort_for_model_id
from yoke.ai.providers.codex_subscription import exception_summary
from yoke.ai.providers.codex_subscription import is_invalid_oauth_token_error
from yoke.ai.providers.codex_subscription import list_provider_models
from yoke.ai.providers.codex_subscription import merge_completed_response
from yoke.ai.providers.codex_subscription import message_phase_from_completed_response
from yoke.ai.providers.codex_subscription import normalize_message_phase
from yoke.ai.providers.usage import parse_token_usage

PROVIDER_NAME = "codex-websockets"
RESPONSES_WEBSOCKETS_BETA = "responses_websockets=2026-02-06"
DEFAULT_WS_BASE_URL = DEFAULT_BASE_URL
STALE_WEBSOCKET_CLOSED_MESSAGE = "Codex WebSocket closed before response.completed."


def register_provider(context: Any) -> CodexWebSockets:
    env = context.env or {}
    cxauth_vault = context.home / DEFAULT_CXAUTH_VAULT_NAME
    model = context.model or env.get("YOKE_CODEX_WEBSOCKETS_MODEL") or "gpt-5.4"
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
                or "600"
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
    function_calls: dict[str, dict[str, str]]
    completed_payload: dict[str, Any] | None = None
    usage_payload: object | None = None
    phase: MessagePhase | None = None


class CodexWebSockets(CodexSubscriptionProvider):
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        config: CodexWebSocketsConfig,
        *,
        websocket_factory: Callable[..., ClientConnection] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(config, sleep=sleep)
        self._websocket_factory = websocket_factory or connect
        self._websocket: ClientConnection | None = None
        self._websocket_credentials: OAuthCredentials | None = None
        self._websocket_auth_profile: str | None = None

    @property
    def config(self) -> CodexWebSocketsConfig:
        return self.__dict__["config"]

    @config.setter
    def config(self, value: CodexWebSocketsConfig) -> None:
        self.__dict__["config"] = value

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        request_started = time.monotonic()
        request_log_id = secrets.token_hex(8)
        request_metrics = self._request_log_metrics(messages, tools)
        payload = self._request_payload(messages, tools)
        payload["type"] = "response.create"
        last_error: ProviderError | None = None

        for attempt in range(self.config.max_retries + 1):
            auth_profile = self._active_auth_profile
            try:
                websocket = self._fresh_websocket()
                websocket.send(json.dumps(payload, separators=(",", ":")))
                message = self._consume_websocket_response(websocket)
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
                self._sleep(self._backoff_seconds(attempt))
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
                self._sleep(self._backoff_seconds(attempt))
            except ProviderError as exc:
                last_error = exc
                self._close_websocket()
                if str(exc) == STALE_WEBSOCKET_CLOSED_MESSAGE:
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
                    self._sleep(self._backoff_seconds(attempt))
                    continue
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
                self._sleep(self._backoff_seconds(attempt))
        if last_error is not None:
            raise last_error
        raise ProviderError("Codex WebSocket request failed without a response.")

    def close(self) -> None:
        self._close_websocket()
        super().close()

    def _request_payload(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        instructions, input_items = convert_messages(messages)
        payload: dict[str, object] = {
            "model": self.config.model,
            "store": False,
            "stream": True,
            "input": input_items,
            "text": {"verbosity": self.config.text_verbosity},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": secrets.token_hex(16),
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
        return payload

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
        }

    def _responses_url(self) -> str:
        return websocket_url_for_base(self.config.base_url)

    def _request_log_metrics(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        metrics = super()._request_log_metrics(messages, tools)
        metrics["transport"] = "websocket"
        return metrics

    def _fresh_websocket(self) -> ClientConnection:
        if self._websocket is not None:
            return self._websocket
        credentials = self._websocket_credentials or self._fresh_credentials()
        self._websocket_credentials = credentials
        self._websocket_auth_profile = self._active_auth_profile
        try:
            self._websocket = self._websocket_factory(
                self._responses_url(),
                additional_headers=self._request_headers(credentials),
                open_timeout=self.config.timeout_seconds,
                close_timeout=min(self.config.timeout_seconds, 10),
                ping_interval=self.config.websocket_ping_interval_seconds,
                ping_timeout=self.config.websocket_ping_timeout_seconds,
                ssl=ssl_context_for_websocket_url(self._responses_url()),
            )
            return self._websocket
        except InvalidStatus as exc:
            raise map_websocket_status_error(exc) from exc
        except Exception as exc:
            raise ProviderError(f"Codex WebSocket connection failed: {exc}") from exc

    def _consume_websocket_response(self, websocket: ClientConnection) -> Message:
        state = CodexWebSocketParseState(text_parts=[], function_calls={})
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            timeout = max(0.1, deadline - time.monotonic())
            try:
                raw = websocket.recv(timeout=timeout)
            except TimeoutError as exc:
                raise ProviderError("Codex WebSocket timed out waiting for response.") from exc
            except ConnectionClosed as exc:
                self._close_websocket()
                raise ProviderError(STALE_WEBSOCKET_CLOSED_MESSAGE) from exc
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            handle_websocket_event(event, state)
            if event.get("type") in {"response.completed", "response.done"}:
                return build_message_from_websocket_state(
                    state,
                    provider_name=self.provider_name,
                    model_id=self.config.model,
                )

    def _close_websocket(self) -> None:
        websocket = self._websocket
        self._websocket = None
        self._websocket_credentials = None
        self._websocket_auth_profile = None
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
        (scheme, parsed.netloc, response_path, parsed.params, parsed.query, parsed.fragment)
    )


def ssl_context_for_websocket_url(url: str) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    context = ssl.create_default_context()
    return context


def handle_websocket_event(
    event: dict[str, Any], state: CodexWebSocketParseState
) -> None:
    event_type = event.get("type")
    if isinstance(event.get("usage"), dict):
        state.usage_payload = event.get("usage")
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
            state.usage_payload = response_payload.get("usage") or state.usage_payload


def handle_websocket_output_item(
    item: dict[str, Any], state: CodexWebSocketParseState
) -> None:
    if item.get("type") == "function_call":
        item_id = str(item.get("id") or item.get("call_id") or len(state.function_calls))
        stored = state.function_calls.setdefault(item_id, {})
        stored["call_id"] = str(item.get("call_id") or item_id)
        stored["name"] = str(item.get("name") or "")
        stored["arguments"] = str(item.get("arguments") or stored.get("arguments") or "{}")
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
                state.text_parts.append(text)


def build_message_from_websocket_state(
    state: CodexWebSocketParseState,
    *,
    provider_name: str,
    model_id: str,
) -> Message:
    if state.completed_payload is not None:
        merge_completed_response(
            state.completed_payload,
            state.text_parts,
            state.function_calls,
        )
        state.usage_payload = state.completed_payload.get("usage") or state.usage_payload
    phase = message_phase_from_completed_response(state.completed_payload) or state.phase
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
        content="".join(state.text_parts) or None,
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
    status_code = getattr(response, "status_code", None) or getattr(response, "status", None)
    message = f"Codex WebSocket handshake failed: {exc}"
    if status_code == 429:
        return ProviderRateLimitError(message)
    if status_code in {500, 502, 503, 504}:
        return ProviderServerError(message, status_code=status_code)
    return ProviderError(message, status_code=status_code if isinstance(status_code, int) else None)


__all__ = [
    "CodexWebSockets",
    "CodexWebSocketsConfig",
    "PROVIDER_NAME",
    "list_provider_models",
    "register_provider",
    "websocket_url_for_base",
]
