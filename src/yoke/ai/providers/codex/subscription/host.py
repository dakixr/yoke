"""Typed host contract for the Codex HTTP request implementation."""

from __future__ import annotations
from collections.abc import Callable
from typing import Protocol
import httpx
from yoke.agent.models import Message
from .config import CodexSubscriptionConfig
from .models import OAuthCredentials


class CodexHTTPHost(Protocol):
    """State and sibling methods consumed by Codex HTTP mixins."""

    @property
    def config(self) -> CodexSubscriptionConfig: ...

    provider_name: str
    _client: httpx.Client
    _owns_client: bool
    _sleep: Callable[[float], None]
    _active_auth_profile: str | None
    _last_logged_auth_profile: str | None
    _prompt_cache_key: str
    _turn_state: str | None

    def _new_client(self) -> httpx.Client: ...

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

    def _request_payload(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]: ...

    def _request_headers(self, credentials: OAuthCredentials) -> dict[str, str]: ...

    def _responses_url(self) -> str: ...

    def _backoff_seconds(
        self, attempt: int, retry_after: float | None = None
    ) -> float: ...

    def _clear_selection_cache(self, *, reason: str = "rate_limit") -> None: ...

    def _delete_account_profile(self, profile_name: str) -> None: ...

    def _delete_fallback_auth(self) -> None: ...

    def _request_log_metrics(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]: ...

    def _log_auth_profile_change(self, auth_source: str, auth_profile: str) -> None: ...

    def _log_request_failure(
        self,
        request_id: str,
        started: float,
        attempt: int,
        exc: Exception,
        auth_profile: str | None,
        request_metrics: dict[str, object],
    ) -> None: ...

    def _log_event(self, event: str, **fields: object) -> None: ...

    def _complete_with_cancel_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message: ...

    def _with_request_cancellation(
        self, action: Callable[[], Message], *, cancel_requested: Callable[[], bool]
    ) -> Message: ...

    def close(self) -> None: ...

    def _set_turn_state(self, value: str) -> None: ...

    def start_turn(self) -> None: ...

    def _new_prompt_cache_key(self) -> str: ...

    def set_session_id(self, session_id: str) -> None: ...

    def generate_image(self, *, prompt: str) -> str: ...

    def edit_image(self, *, prompt: str, image_urls: list[str]) -> str: ...

    def _generate_hosted_image(
        self, *, prompt: str, reference_image_urls: list[str]
    ) -> str: ...

    def _hosted_image_payload(
        self, *, prompt: str, reference_image_urls: list[str]
    ) -> dict[str, object]: ...
