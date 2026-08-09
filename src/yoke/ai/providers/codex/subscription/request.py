"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from typing import Any

import contextlib
import platform
import secrets
import time


from yoke.agent.models import Message

from .catalog import (
    OAUTH_PROVIDER_ID,
    X_CODEX_TURN_STATE_HEADER,
    X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER,
    originator_for_model,
    uses_responses_lite,
)
from .helpers import clamp_reasoning_effort
from .logging import exception_summary, log_provider_event
from .messages import convert_messages, convert_tools, count_message_images
from .models import OAuthCredentials
from .oauth import login_openai_codex, refresh_openai_codex_token
from .profiles import AuthStorage, CodexProfileStore


class CodexRequestMixin:
    def _fresh_credentials(self: Any) -> OAuthCredentials:
        if self.config.accounts_dir.expanduser().exists():
            try:
                store = CodexProfileStore(
                    self.config.accounts_dir,
                    self.config.auths_path,
                    self.config.selection_path,
                    self.config.selection_ttl_seconds,
                )
                credentials, profile_name = store.fresh_credentials_with_profile()
                self._active_auth_profile = profile_name
                self._log_auth_profile_change("accounts_dir", profile_name)
                return credentials
            except Exception as exc:
                self._log_event(
                    "auth_fallback",
                    from_source="accounts_dir",
                    to_source="auth_path",
                    reason=exception_summary(exc),
                )

        self._active_auth_profile = "auth_path"
        self._log_auth_profile_change("auth_path", "auth_path")
        storage = AuthStorage(self.config.auth_path)
        credentials = storage.get_oauth(OAUTH_PROVIDER_ID)
        if credentials is None:
            self._log_event("auth_login_required", auth_source="auth_path")
            credentials = login_openai_codex(self.config.originator)
            storage.set_oauth(OAUTH_PROVIDER_ID, credentials)
            return credentials
        if credentials.expires - int(time.time() * 1000) > 60_000:
            return credentials
        self._log_event("token_refresh_start", auth_source="auth_path")
        return storage.refresh_oauth_with_lock(
            OAUTH_PROVIDER_ID,
            lambda current: refresh_openai_codex_token(current),
        )

    def _recover_invalid_oauth_credentials(
        self: Any,
        *,
        auth_profile: str | None,
        request_id: str,
        attempt: int,
        detail: str,
        request_metrics: dict[str, object],
    ) -> OAuthCredentials | None:
        self._log_event(
            "auth_invalidated",
            request_id=request_id,
            attempt=attempt,
            auth_profile=auth_profile,
            detail=detail,
            **request_metrics,
        )
        self._clear_selection_cache(reason="invalid_oauth_token")

        if auth_profile is not None and auth_profile != "auth_path":
            self._delete_account_profile(auth_profile)

        if self.config.accounts_dir.expanduser().exists():
            try:
                credentials = self._fresh_credentials()
            except Exception as exc:
                self._log_event(
                    "auth_fallback",
                    from_source="accounts_dir",
                    to_source="auth_path",
                    reason=exception_summary(exc),
                )
            else:
                return credentials

        self._delete_fallback_auth()
        storage = AuthStorage(self.config.auth_path)
        credentials = login_openai_codex(self.config.originator)
        storage.set_oauth(OAUTH_PROVIDER_ID, credentials)
        self._active_auth_profile = "auth_path"
        self._log_auth_profile_change("auth_path", "auth_path")
        return credentials

    def _request_payload(
        self: Any, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        instructions, input_items = convert_messages(messages)
        responses_lite = uses_responses_lite(self.config.model)
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
            "prompt_cache_key": self._prompt_cache_key,
            "tool_choice": "auto",
            "parallel_tool_calls": not responses_lite,
            "reasoning": {
                "effort": clamp_reasoning_effort(
                    self.config.model, self.config.reasoning_effort
                ),
                "summary": "auto",
                **({"context": "all_turns"} if responses_lite else {}),
            },
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = convert_tools(tools)
        if client_metadata:
            payload["client_metadata"] = client_metadata
        return payload

    def _request_headers(self: Any, credentials: OAuthCredentials) -> dict[str, str]:
        request_id = secrets.token_hex(16)
        return {
            "Authorization": f"Bearer {credentials.access}",
            "chatgpt-account-id": credentials.account_id,
            "originator": originator_for_model(
                self.config.model, self.config.originator
            ),
            "User-Agent": (
                f"yoke ({platform.system().lower()}; {platform.machine().lower()})"
            ),
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "session_id": request_id,
            "x-client-request-id": request_id,
            **(
                {X_CODEX_TURN_STATE_HEADER: self._turn_state}
                if self._turn_state
                else {}
            ),
            **(
                {X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER: "true"}
                if uses_responses_lite(self.config.model)
                else {}
            ),
        }

    def _responses_url(self: Any) -> str:
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/codex/responses"):
            return base_url
        if base_url.endswith("/codex"):
            return f"{base_url}/responses"
        return f"{base_url}/codex/responses"

    def _backoff_seconds(self: Any, attempt: int) -> float:
        return min(
            self.config.retry_backoff_seconds * (2**attempt),
            self.config.max_retry_backoff_seconds,
        )

    def _clear_selection_cache(self: Any, *, reason: str = "rate_limit") -> None:
        """Clear the cached profile selection to force account rotation on the next credential fetch."""
        self._turn_state = None
        selection_path = self.config.selection_path.expanduser()
        with contextlib.suppress(FileNotFoundError):
            selection_path.unlink()
        self._log_event("account_rotation", reason=reason)

    def _delete_account_profile(self: Any, profile_name: str) -> None:
        path = self.config.accounts_dir.expanduser() / profile_name / "auth.json"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _delete_fallback_auth(self: Any) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.config.auth_path.expanduser().unlink()

    def _request_log_metrics(
        self: Any, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
            "message_count": len(messages),
            "tool_count": len(tools),
            "image_count": count_message_images(messages),
            "max_retries": self.config.max_retries,
        }

    def _log_auth_profile_change(
        self: Any, auth_source: str, auth_profile: str
    ) -> None:
        if self._last_logged_auth_profile == auth_profile:
            return
        self._last_logged_auth_profile = auth_profile
        self._log_event(
            "auth_profile_changed",
            auth_source=auth_source,
            auth_profile=auth_profile,
        )

    def _log_request_failure(
        self: Any,
        request_id: str,
        started: float,
        attempt: int,
        exc: Exception,
        auth_profile: str | None,
        request_metrics: dict[str, object],
    ) -> None:
        self._log_event(
            "request_error",
            request_id=request_id,
            attempt=attempt,
            duration_seconds=round(time.monotonic() - started, 3),
            error=exception_summary(exc),
            status_code=getattr(exc, "status_code", None),
            auth_profile=auth_profile,
            **request_metrics,
        )

    def _log_event(self: Any, event: str, **fields: object) -> None:
        log_provider_event(self.config.logs_dir, self.provider_name, event, **fields)
