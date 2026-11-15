"""Typed host contract for Codex WebSocket connection and replay state."""

from __future__ import annotations
from collections.abc import Callable
from typing import Any, Protocol
from yoke.agent.models import Message
from yoke.ai.providers.codex.response_state import CodexResponseChain
from yoke.ai.providers.codex.subscription.host import CodexHTTPHost
from yoke.ai.providers.codex.subscription.models import OAuthCredentials
from .config import CodexConfig, CodexWebSocketConnection


class CodexWebSocketHost(CodexHTTPHost, Protocol):
    """State and sibling methods consumed by the WebSocket transport mixin."""

    @property
    def config(self) -> CodexConfig: ...

    _websocket: CodexWebSocketConnection | None
    _websocket_factory: Callable[..., CodexWebSocketConnection]
    _websocket_credentials: OAuthCredentials | None
    _websocket_auth_profile: str | None
    _stream_started: bool
    _response_chain: CodexResponseChain

    def close(self) -> None: ...

    def set_session_id(self, session_id: str) -> None: ...

    def _request_payload(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]: ...

    def _prepare_websocket_payload(
        self, payload: dict[str, object]
    ) -> dict[str, object]: ...

    def _remember_successful_response(
        self, payload: dict[str, object], message: Message
    ) -> None: ...

    def _request_headers(self, credentials: OAuthCredentials) -> dict[str, str]: ...

    def _fresh_credentials(self) -> OAuthCredentials: ...

    def _recover_invalid_oauth_credentials(
        self,
        *,
        auth_profile: str | None,
        request_id: str,
        attempt: int,
        detail: str,
        request_metrics: dict[str, object],
    ) -> OAuthCredentials | None: ...

    def _responses_url(self) -> str: ...

    def _request_log_metrics(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]: ...

    def _fresh_websocket(self) -> CodexWebSocketConnection: ...

    def _websocket_closed(self, websocket: CodexWebSocketConnection) -> bool: ...

    def _valid_websocket_credentials(self) -> OAuthCredentials | None: ...

    def _selected_auth_profile(self) -> str | None: ...

    def _consume_websocket_response(
        self,
        websocket: CodexWebSocketConnection,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Message: ...

    def _capture_turn_state(self, event: dict[str, Any]) -> None: ...

    def _close_websocket(self, *, clear_credentials: bool = True) -> None: ...
