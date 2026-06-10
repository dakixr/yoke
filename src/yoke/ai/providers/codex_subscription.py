"""Codex subscription provider plugin for the YOKE harness."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import platform
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from yoke.agent.models import (
    Message,
    MessagePhase,
    ToolCall,
    ToolFunction,
)
from yoke.ai.providers.base import (
    Provider,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.model_selection import (
    default_reasoning_effort_for_model,
)
from yoke.ai.providers.openai_compat import serialize_message_for_openai
from yoke.ai.providers.usage import parse_token_usage
from pydantic import BaseModel

PROVIDER_NAME = "codex"

OAUTH_PROVIDER_ID = "openai-codex"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_CXAUTH_VAULT_NAME = ".codex-auth"
DEFAULT_LOGS_DIR = Path.home() / ".yoke" / "providers" / "logs"
MODEL_CATALOG = (
    ProviderModelInfo(
        id="gpt-5.5",
        display_name="GPT-5.5",
        context_window_tokens=300_000,
        thinking_levels=("low", "medium", "high", "xhigh"),
        default_thinking_level="low",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-5.4",
        display_name="GPT-5.4",
        context_window_tokens=300_000,
        thinking_levels=("low", "medium", "high", "xhigh"),
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-5.4-mini",
        display_name="GPT-5.4 Mini",
        context_window_tokens=300_000,
        thinking_levels=("low", "medium", "high", "xhigh"),
        default_thinking_level="xhigh",
        supports_image_inputs=True,
    ),
)


def list_provider_models(context: Any) -> list[ProviderModelInfo]:
    del context
    return [model.model_copy(deep=True) for model in MODEL_CATALOG]


def default_reasoning_effort_for_model_id(model_id: str) -> str:
    for model in MODEL_CATALOG:
        if model.id == model_id.strip():
            return default_reasoning_effort_for_model(model) or "medium"
    return "medium"


def register_provider(context: Any) -> CodexSubscriptionProvider:
    env = context.env or os.environ
    cxauth_vault = context.home / DEFAULT_CXAUTH_VAULT_NAME
    return CodexSubscriptionProvider(
        CodexSubscriptionConfig(
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
            model=(context.model or env.get("YOKE_CODEX_MODEL") or "gpt-5.4"),
            base_url=(env.get("YOKE_CODEX_BASE_URL") or DEFAULT_BASE_URL),
            originator=env.get("YOKE_CODEX_ORIGINATOR") or "yoke",
            timeout_seconds=float(env.get("YOKE_CODEX_TIMEOUT_SECONDS") or "600"),
            max_retries=int(env.get("YOKE_CODEX_MAX_RETRIES") or "5"),
            reasoning_effort=(
                context.reasoning_effort
                or env.get("YOKE_CODEX_REASONING_EFFORT")
                or default_reasoning_effort_for_model_id(
                    context.model or env.get("YOKE_CODEX_MODEL") or "gpt-5.4"
                )
            ),
            text_verbosity=(env.get("YOKE_CODEX_TEXT_VERBOSITY") or "medium"),
            logs_dir=Path(
                env.get("YOKE_CODEX_LOGS_DIR")
                or env.get("YOKE_PROVIDER_LOGS_DIR")
                or str(DEFAULT_LOGS_DIR)
            ),
        )
    )


class CodexSubscriptionConfig(BaseModel):
    auth_path: Path
    accounts_dir: Path
    auths_path: Path
    selection_path: Path
    selection_ttl_seconds: int = 1800
    model: str = "gpt-5.4"
    base_url: str = DEFAULT_BASE_URL
    originator: str = "yoke"
    timeout_seconds: float = 600.0
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 15.0
    reasoning_effort: str = "medium"
    text_verbosity: str = "medium"
    logs_dir: Path = DEFAULT_LOGS_DIR


@dataclass(slots=True)
class OAuthCredentials:
    access: str
    refresh: str
    expires: int
    account_id: str

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> OAuthCredentials:
        access = payload.get("access")
        refresh = payload.get("refresh")
        expires = payload.get("expires")
        account_id = payload.get("accountId")
        if not isinstance(access, str) or not access:
            raise ValueError("Stored Codex auth is missing an access token.")
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("Stored Codex auth is missing a refresh token.")
        if not isinstance(expires, int | float):
            raise ValueError("Stored Codex auth is missing expiry metadata.")
        if not isinstance(account_id, str) or not account_id:
            account_id = account_id_from_access_token(access)
        return cls(
            access=access,
            refresh=refresh,
            expires=int(expires),
            account_id=account_id,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "type": "oauth",
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
            "accountId": self.account_id,
        }


class CodexSubscriptionProvider(Provider):
    provider_name = PROVIDER_NAME
    supports_image_inputs = True
    max_images_per_message = None

    def __init__(
        self,
        config: CodexSubscriptionConfig,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._sleep = sleep or time.sleep
        self._active_auth_profile: str | None = None
        self._last_logged_auth_profile: str | None = None
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=config.timeout_seconds,
            verify=False,  # noqa: S501
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return [model.model_copy(deep=True) for model in MODEL_CATALOG]

    def current_model_id(self) -> str | None:
        model = self.config.model.strip()
        return model or None

    def current_model_info(self) -> ProviderModelInfo | None:
        current_model = self.current_model_id()
        if current_model is None:
            return None
        for model in self.list_models():
            if model.id == current_model:
                return model
        return None

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        normalized_model = model_id.strip()
        if not normalized_model:
            raise ValueError("model_id must be a non-empty string")
        available = {model.id: model for model in self.list_models()}
        selected = available.get(normalized_model)
        if selected is None:
            choices = ", ".join(sorted(available))
            raise ValueError(
                f"Unknown model {normalized_model!r} for provider 'codex'. "
                f"Available: {choices}."
            )
        if reasoning_effort is not None:
            normalized_reasoning = reasoning_effort.strip().lower()
            if normalized_reasoning not in selected.thinking_levels:
                allowed = ", ".join(selected.thinking_levels)
                raise ValueError(
                    f"Unsupported reasoning effort {reasoning_effort!r} for "
                    f"model {normalized_model!r}. Allowed: {allowed}."
                )
            self.config.reasoning_effort = normalized_reasoning
        else:
            self.config.reasoning_effort = (
                default_reasoning_effort_for_model(selected) or "medium"
            )
        self.config.model = normalized_model

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
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
                            self._sleep(wait_seconds)
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
                            self._sleep(wait_seconds)
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
                            if recovered is not None and attempt < self.config.max_retries:
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
                    self._sleep(wait_seconds)
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
                    self._sleep(wait_seconds)
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
                    self._sleep(wait_seconds)
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
            except httpx.RequestError as exc:
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

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _fresh_credentials(self) -> OAuthCredentials:
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
        self,
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

        if auth_profile not in {None, "auth_path"}:
            self._delete_account_profile(auth_profile)
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
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "session_id": request_id,
            "x-client-request-id": request_id,
        }

    def _responses_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/codex/responses"):
            return base_url
        if base_url.endswith("/codex"):
            return f"{base_url}/responses"
        return f"{base_url}/codex/responses"

    def _backoff_seconds(self, attempt: int) -> float:
        return min(
            self.config.retry_backoff_seconds * (2**attempt),
            self.config.max_retry_backoff_seconds,
        )

    def _clear_selection_cache(self, *, reason: str = "rate_limit") -> None:
        """Clear the cached profile selection to force account rotation on the next credential fetch."""
        selection_path = self.config.selection_path.expanduser()
        with contextlib.suppress(FileNotFoundError):
            selection_path.unlink()
        self._log_event("account_rotation", reason=reason)

    def _delete_account_profile(self, profile_name: str) -> None:
        path = self.config.accounts_dir.expanduser() / profile_name / "auth.json"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _delete_fallback_auth(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.config.auth_path.expanduser().unlink()

    def _request_log_metrics(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
            "message_count": len(messages),
            "tool_count": len(tools),
            "image_count": count_message_images(messages),
            "max_retries": self.config.max_retries,
        }

    def _log_auth_profile_change(self, auth_source: str, auth_profile: str) -> None:
        if self._last_logged_auth_profile == auth_profile:
            return
        self._last_logged_auth_profile = auth_profile
        self._log_event(
            "auth_profile_changed",
            auth_source=auth_source,
            auth_profile=auth_profile,
        )

    def _log_request_failure(
        self,
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

    def _log_event(self, event: str, **fields: object) -> None:
        log_provider_event(self.config.logs_dir, self.provider_name, event, **fields)


@dataclass(slots=True)
class CodexProfile:
    name: str
    payload: dict[str, Any]

    def credentials(self) -> OAuthCredentials:
        tokens = self._tokens()
        access = _required_str(tokens, "access_token", self.name)
        refresh = _required_str(tokens, "refresh_token", self.name)
        account_id = tokens.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            account_id = account_id_from_access_token(access)
        expires = _jwt_exp_millis(access)
        return OAuthCredentials(
            access=access,
            refresh=refresh,
            expires=expires,
            account_id=account_id,
        )

    def with_credentials(self, credentials: OAuthCredentials) -> dict[str, Any]:
        updated = dict(self.payload)
        tokens = dict(self._tokens())
        tokens["access_token"] = credentials.access
        tokens["refresh_token"] = credentials.refresh
        tokens["account_id"] = credentials.account_id
        updated["tokens"] = tokens
        updated["last_refresh"] = _utc_now_iso()
        return updated

    def _tokens(self) -> dict[str, Any]:
        tokens = self.payload.get("tokens")
        if not isinstance(tokens, dict):
            raise ProviderError(f"Codex profile {self.name!r} is missing tokens.")
        return tokens


@dataclass(slots=True)
class QuotaWindow:
    used_percent: int | None
    resets_at: int | None
    duration_mins: int | None


@dataclass(slots=True)
class QuotaLimit:
    primary: QuotaWindow | None
    secondary: QuotaWindow | None


@dataclass(slots=True)
class QuotaSnapshot:
    default_limit: QuotaLimit | None
    updated_auth: dict[str, Any]


@dataclass(slots=True)
class Pace:
    delta_percent: float
    resets_in_seconds: float


@dataclass(slots=True)
class AccountScore:
    score: float
    rejected: bool


class CodexProfileStore:
    def __init__(
        self,
        accounts_dir: Path,
        auths_path: Path,
        selection_path: Path,
        ttl_seconds: int,
    ) -> None:
        self.accounts_dir = accounts_dir.expanduser().resolve()
        self.auths_path = auths_path.expanduser().resolve()
        self.selection_path = selection_path.expanduser().resolve()
        self.ttl_seconds = ttl_seconds
        self.lock_path = self.selection_path.with_suffix(
            self.selection_path.suffix + ".lock"
        )

    def fresh_credentials(self) -> OAuthCredentials:
        credentials, _profile_name = self.fresh_credentials_with_profile()
        return credentials

    def fresh_credentials_with_profile(self) -> tuple[OAuthCredentials, str]:
        with self._lock():
            profiles = self._read_profiles()
            profile = self._cached_profile(profiles)
            if profile is None:
                profile = self._select_best_profile(profiles)
                self._write_selection(profile.name)
            credentials = profile.credentials()
            if credentials.expires - int(time.time() * 1000) > 60_000:
                return credentials, profile.name
            refreshed = refresh_openai_codex_token(credentials)
            self._write_profile(profile.name, profile.with_credentials(refreshed))
            return refreshed, profile.name

    def _cached_profile(self, profiles: dict[str, CodexProfile]) -> CodexProfile | None:
        selection = self._read_selection()
        name = selection.get("selected_profile")
        selected_at = selection.get("selected_at")
        if not isinstance(name, str) or not isinstance(selected_at, int | float):
            return None
        if time.time() - float(selected_at) > self.ttl_seconds:
            return None
        return profiles.get(name)

    def _select_best_profile(self, profiles: dict[str, CodexProfile]) -> CodexProfile:
        best_profile: CodexProfile | None = None
        best_score = float("inf")
        failures: list[str] = []
        for profile in profiles.values():
            try:
                snapshot = query_codex_quota(profile.payload)
                self._write_profile(profile.name, snapshot.updated_auth)
                account_score = score_quota_snapshot(snapshot)
            except Exception as exc:
                failures.append(f"{profile.name}: {exc}")
                continue
            if account_score.rejected:
                continue
            if account_score.score < best_score:
                best_profile = CodexProfile(profile.name, snapshot.updated_auth)
                best_score = account_score.score
        if best_profile is not None:
            return best_profile
        cached_name = self._read_selection().get("selected_profile")
        if isinstance(cached_name, str) and cached_name in profiles:
            return profiles[cached_name]
        details = "; ".join(failures) if failures else "no profiles configured"
        raise ProviderError(f"No usable Codex profile found: {details}")

    def _read_profiles(self) -> dict[str, CodexProfile]:
        return self._read_account_profiles()

    def _read_account_profiles(self) -> dict[str, CodexProfile]:
        profiles: dict[str, CodexProfile] = {}
        if not self.accounts_dir.exists():
            return profiles
        for path in sorted(self.accounts_dir.glob("*/auth.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"Unable to parse Codex auth profile {path}."
                ) from exc
            if not isinstance(payload, dict):
                raise ProviderError(f"Codex auth profile {path} is invalid.")
            profiles[path.parent.name] = CodexProfile(path.parent.name, payload)
        return profiles

    def _read_legacy_profiles(self) -> dict[str, CodexProfile]:
        try:
            payload = json.loads(self.auths_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProviderError(
                f"Missing Codex auth profiles file {self.auths_path}."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Unable to parse Codex auth profiles file {self.auths_path}."
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                f"Codex auth profiles file {self.auths_path} is invalid."
            )
        profiles: dict[str, CodexProfile] = {}
        for name, value in payload.items():
            if isinstance(name, str) and isinstance(value, dict):
                profiles[name] = CodexProfile(name, value)
        return profiles

    def _write_profile(self, name: str, payload: dict[str, Any]) -> None:
        account_path = self.accounts_dir / name / "auth.json"
        if account_path.exists() or self.accounts_dir.exists():
            self._atomic_write(account_path, payload)
            return
        profiles = json.loads(self.auths_path.read_text(encoding="utf-8"))
        if not isinstance(profiles, dict):
            raise ProviderError(
                f"Codex auth profiles file {self.auths_path} is invalid."
            )
        profiles[name] = payload
        self._atomic_write(self.auths_path, profiles)

    def _read_selection(self) -> dict[str, Any]:
        if not self.selection_path.exists():
            return {}
        try:
            payload = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_selection(self, profile_name: str) -> None:
        self._atomic_write(
            self.selection_path,
            {"selected_profile": profile_name, "selected_at": time.time()},
        )

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        temporary.replace(path)

    @contextlib.contextmanager
    def _lock(self) -> Any:
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30
        handle: int | None = None
        while handle is None:
            try:
                handle = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"Timed out waiting for Codex profile lock {self.lock_path}."
                    ) from None
                time.sleep(0.1)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()


class AuthStorage:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def get_oauth(self, provider_id: str) -> OAuthCredentials | None:
        payload = self._read()
        raw = payload.get(provider_id)
        if not isinstance(raw, dict):
            return None
        if raw.get("type") != "oauth":  # ty:ignore[invalid-argument-type]
            return None
        return OAuthCredentials.from_json(raw)  # ty:ignore[invalid-argument-type]

    def set_oauth(self, provider_id: str, credentials: OAuthCredentials) -> None:
        with self._lock():
            payload = self._read()
            payload[provider_id] = credentials.to_json()
            self._write(payload)

    def refresh_oauth_with_lock(
        self,
        provider_id: str,
        refresher: Callable[[OAuthCredentials], OAuthCredentials],
    ) -> OAuthCredentials:
        with self._lock():
            current = self.get_oauth(provider_id)
            if current is None:
                current = login_openai_codex("yoke")
                self._write_provider(provider_id, current)
                return current
            if current.expires - int(time.time() * 1000) > 60_000:
                return current
            refreshed = refresher(current)
            self._write_provider(provider_id, refreshed)
            return refreshed

    def _write_provider(self, provider_id: str, credentials: OAuthCredentials) -> None:
        payload = self._read()
        payload[provider_id] = credentials.to_json()
        self._write(payload)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Unable to parse Codex auth file {self.path}."
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Codex auth file {self.path} is invalid.")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @contextlib.contextmanager
    def _lock(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30
        handle: int | None = None
        while handle is None:
            try:
                handle = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"Timed out waiting for auth lock {self.lock_path}."
                    ) from None
                time.sleep(0.1)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()


@dataclass(slots=True)
class AuthorizationFlow:
    url: str
    verifier: str
    state: str


def login_openai_codex(originator: str) -> OAuthCredentials:
    flow = create_authorization_flow(originator)
    print("Open this URL to sign in with your ChatGPT Codex subscription:")
    print(flow.url)
    with contextlib.suppress(Exception):
        webbrowser.open(flow.url)
    callback = wait_for_callback(flow.state)
    if callback is None:
        print("Paste the full redirect URL or authorization code below.")
        callback = parse_authorization_input(input("Authorization: "), flow.state)
    return exchange_authorization_code(callback, flow.verifier)


def create_authorization_flow(originator: str) -> AuthorizationFlow:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return AuthorizationFlow(
        url=f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        verifier=verifier,
        state=state,
    )


@dataclass(slots=True)
class AuthorizationCallback:
    code: str
    state: str | None = None


def wait_for_callback(expected_state: str) -> AuthorizationCallback | None:
    host = os.getenv("YOKE_OAUTH_CALLBACK_HOST", "127.0.0.1")
    parsed = urllib.parse.urlparse(REDIRECT_URI)
    port = parsed.port or 1455
    route = parsed.path
    result: dict[str, AuthorizationCallback | Exception] = {}
    done = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:  # ty:ignore[invalid-method-override]
            return

        def do_GET(self) -> None:  # noqa: N802
            request = urllib.parse.urlparse(self.path)
            if request.path != route:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(request.query)
            code = first_query_value(query, "code")
            state = first_query_value(query, "state")
            if not code:
                result["value"] = ProviderError("OAuth callback missed code.")
                self._html(400, "Codex login failed. Missing code.")
                done.set()
                return
            if state != expected_state:
                result["value"] = ProviderError("OAuth state mismatch.")
                self._html(400, "Codex login failed. State mismatch.")
                done.set()
                return
            result["value"] = AuthorizationCallback(code=code, state=state)
            self._html(200, "Codex login complete. You can close this tab.")
            done.set()

        def _html(self, status: int, message: str) -> None:
            body = (
                f"<!doctype html><html><body><p>{message}</p></body></html>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = http.server.HTTPServer((host, port), CallbackHandler)
    except OSError:
        return None
    server.timeout = 0.2

    def serve() -> None:
        while not done.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        while not done.wait(0.2):
            pass
    except KeyboardInterrupt:
        return None
    finally:
        server.server_close()
    value = result.get("value")
    if isinstance(value, Exception):
        raise value
    return value


def parse_authorization_input(
    raw_value: str, expected_state: str
) -> AuthorizationCallback:
    value = raw_value.strip()
    if not value:
        raise ProviderError("Authorization input was empty.")
    if "#" in value and not value.startswith("http"):
        code, state = value.split("#", 1)
        callback = AuthorizationCallback(code=code.strip(), state=state.strip())
    elif value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        query = urllib.parse.parse_qs(parsed.query)
        callback = AuthorizationCallback(
            code=first_query_value(query, "code") or "",
            state=first_query_value(query, "state"),
        )
    elif value.startswith("code=") or "&code=" in value:
        query = urllib.parse.parse_qs(value.lstrip("?"))
        callback = AuthorizationCallback(
            code=first_query_value(query, "code") or "",
            state=first_query_value(query, "state"),
        )
    else:
        callback = AuthorizationCallback(code=value)
    if not callback.code:
        raise ProviderError("Authorization input did not include a code.")
    if callback.state is not None and callback.state != expected_state:
        raise ProviderError("OAuth state mismatch.")
    return callback


def exchange_authorization_code(
    callback: AuthorizationCallback, verifier: str
) -> OAuthCredentials:
    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": callback.code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
    return token_request(body)


def refresh_openai_codex_token(
    credentials: OAuthCredentials,
) -> OAuthCredentials:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": credentials.refresh,
        "client_id": CLIENT_ID,
    }
    return token_request(body)


def token_request(body: dict[str, str]) -> OAuthCredentials:
    try:
        response = httpx.post(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=60,
            verify=False,  # noqa: S501
        )
    except httpx.RequestError as exc:
        raise ProviderError(f"Codex token request failed: {exc}") from exc
    if response.is_error:
        raise ProviderError(f"Codex token request failed: {error_detail(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError("Codex token endpoint returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderError("Codex token endpoint returned invalid payload.")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access, str) or not access:
        raise ProviderError("Codex token endpoint missed access_token.")
    if not isinstance(refresh, str) or not refresh:
        raise ProviderError("Codex token endpoint missed refresh_token.")
    if not isinstance(expires_in, int | float):
        raise ProviderError("Codex token endpoint missed expires_in.")
    account_id = account_id_from_access_token(access)
    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=int(time.time() * 1000 + float(expires_in) * 1000),
        account_id=account_id,
    )


def account_id_from_access_token(access_token: str) -> str:
    try:
        payload_segment = access_token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("Unable to decode Codex access token.") from exc
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    raise ProviderError("Codex access token does not include an account ID.")


def query_codex_quota(auth_data: dict[str, Any]) -> QuotaSnapshot:
    credentials = _credentials_from_codex_auth_payload(auth_data)
    updated_auth = auth_data
    if credentials.expires - int(time.time() * 1000) <= 60_000:
        credentials = refresh_openai_codex_token(credentials)
        updated_auth = _codex_auth_payload_with_credentials(auth_data, credentials)
    usage = _fetch_codex_oauth_usage(credentials)
    return QuotaSnapshot(
        default_limit=_parse_oauth_usage_limit(usage), updated_auth=updated_auth
    )


def score_quota_snapshot(snapshot: QuotaSnapshot) -> AccountScore:
    limit = snapshot.default_limit
    session = limit.primary if limit else None
    weekly = limit.secondary if limit else None
    session_used = session.used_percent if session else None
    weekly_used = weekly.used_percent if weekly else None
    session_pace = _pace_for_window(session, default_window_minutes=300)
    weekly_pace = _pace_for_window(weekly, default_window_minutes=10080)
    if weekly_used is not None and weekly_used >= 98:
        return AccountScore(score=float("inf"), rejected=True)
    if session_used is not None and session_used >= 98:
        if session_pace is None or session_pace.resets_in_seconds > 10 * 60:
            return AccountScore(score=float("inf"), rejected=True)
    score = 0.0
    score += float(session_used if session_used is not None else 999)
    score += float(weekly_used if weekly_used is not None else 999) * 2
    score += _pace_pressure(session_pace, deficit_weight=1.5, reserve_weight=0.5)
    score += _pace_pressure(weekly_pace, deficit_weight=3.0, reserve_weight=1.0)
    return AccountScore(score=score, rejected=False)


def _pace_for_window(
    window: QuotaWindow | None, *, default_window_minutes: int
) -> Pace | None:
    if window is None or window.used_percent is None or window.resets_at is None:
        return None
    window_minutes = window.duration_mins or default_window_minutes
    if window_minutes <= 0:
        return None
    duration = float(window_minutes * 60)
    resets_in = float(window.resets_at) - time.time()
    if resets_in <= 0 or resets_in > duration:
        return None
    elapsed = max(0.0, min(duration, duration - resets_in))
    actual = max(0.0, min(float(window.used_percent), 100.0))
    expected = max(0.0, min((elapsed / duration) * 100.0, 100.0))
    return Pace(delta_percent=actual - expected, resets_in_seconds=resets_in)


def _pace_pressure(
    pace: Pace | None, *, deficit_weight: float, reserve_weight: float
) -> float:
    if pace is None:
        return 0.0
    if pace.delta_percent > 0:
        return pace.delta_percent * deficit_weight
    return pace.delta_percent * reserve_weight


def _parse_quota_limit(raw: Any) -> QuotaLimit | None:
    if not isinstance(raw, dict):
        return None
    return QuotaLimit(
        primary=_parse_quota_window(raw.get("primary")),
        secondary=_parse_quota_window(raw.get("secondary")),
    )


def _credentials_from_codex_auth_payload(
    auth_data: dict[str, Any],
) -> OAuthCredentials:
    return CodexProfile("quota-probe", auth_data).credentials()


def _codex_auth_payload_with_credentials(
    auth_data: dict[str, Any], credentials: OAuthCredentials
) -> dict[str, Any]:
    return CodexProfile("quota-probe", auth_data).with_credentials(credentials)


def _fetch_codex_oauth_usage(credentials: OAuthCredentials) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {credentials.access}",
        "ChatGPT-Account-Id": credentials.account_id,
        "User-Agent": "yoke",
        "Accept": "application/json",
    }
    try:
        response = httpx.get(
            DEFAULT_USAGE_URL,
            headers=headers,
            timeout=30,
            verify=False,  # noqa: S501
        )
    except httpx.RequestError as exc:
        raise ProviderError(f"Codex OAuth usage request failed: {exc}") from exc
    if response.is_error:
        raise ProviderError(
            f"Codex OAuth usage request failed: {error_detail(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            "Codex OAuth usage endpoint returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError("Codex OAuth usage endpoint returned invalid payload.")
    return payload


def _parse_oauth_usage_limit(payload: dict[str, Any]) -> QuotaLimit | None:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    return _normalize_quota_limit(
        QuotaLimit(
            primary=_parse_oauth_usage_window(rate_limit.get("primary_window")),
            secondary=_parse_oauth_usage_window(rate_limit.get("secondary_window")),
        )
    )


def _parse_oauth_usage_window(raw: Any) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    resets = raw.get("reset_at")
    duration_seconds = raw.get("limit_window_seconds")
    return QuotaWindow(
        used_percent=used if isinstance(used, int) else None,
        resets_at=resets if isinstance(resets, int) else None,
        duration_mins=(
            duration_seconds // 60 if isinstance(duration_seconds, int) else None
        ),
    )


def _normalize_quota_limit(limit: QuotaLimit) -> QuotaLimit | None:
    primary = limit.primary
    secondary = limit.secondary
    if primary is None and secondary is None:
        return None
    primary_role = _quota_window_role(primary)
    secondary_role = _quota_window_role(secondary)
    if primary is not None and secondary is not None:
        if primary_role == "weekly" and secondary_role in {
            "session",
            "unknown",
        }:
            return QuotaLimit(primary=secondary, secondary=primary)
        return limit
    if primary is not None and primary_role == "weekly":
        return QuotaLimit(primary=None, secondary=primary)
    if secondary is not None and secondary_role in {"session", "unknown"}:
        return QuotaLimit(primary=secondary, secondary=None)
    return limit


def _quota_window_role(window: QuotaWindow | None) -> str:
    if window is None:
        return "none"
    if window.duration_mins == 300:
        return "session"
    if window.duration_mins == 10080:
        return "weekly"
    return "unknown"


def _parse_quota_window(raw: Any) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("usedPercent")
    resets = raw.get("resetsAt")
    duration = raw.get("windowDurationMins")
    return QuotaWindow(
        used_percent=used if isinstance(used, int) else None,
        resets_at=resets if isinstance(resets, int) else None,
        duration_mins=duration if isinstance(duration, int) else None,
    )


def _required_str(payload: dict[str, Any], key: str, profile_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderError(f"Codex profile {profile_name!r} is missing tokens.{key}.")
    return value


def _jwt_exp_millis(token: str) -> int:
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("Unable to decode Codex access token expiry.") from exc
    expires = payload.get("exp")
    if not isinstance(expires, int | float):
        raise ProviderError("Codex access token does not include expiry metadata.")
    return int(float(expires) * 1000)


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def convert_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            text = message.text_content()
            if text:
                instructions.append(text)
            continue
        if message.role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": message.text_content() or "",
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            text = message.text_content()
            if text:
                input_items.append(message_item(message.role, text))
            for tool_call in message.tool_calls:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    }
                )
            continue
        input_items.append(convert_text_message(message))
    return "\n\n".join(instructions), input_items


def convert_text_message(message: Message) -> dict[str, Any]:
    serialized = serialize_message_for_openai(message)
    content = serialized.get("content", "")
    if isinstance(content, list):
        converted_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":  # ty:ignore[invalid-argument-type]
                converted_parts.append(
                    {"type": "input_text", "text": part.get("text", "")}  # ty:ignore[no-matching-overload]
                )
            elif part.get("type") == "image_url":  # ty:ignore[invalid-argument-type]
                converted_parts.append(
                    {
                        "type": "input_image",
                        "image_url": part.get("image_url", {}).get("url", ""),  # ty:ignore[no-matching-overload]
                    }
                )
        return {"role": message.role, "content": converted_parts}
    return message_item(message.role, str(content or ""))


def message_item(role: str, text: str) -> dict[str, Any]:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def convert_tools(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = []
    for tool in tools:
        if tool.get("type") == "function":
            function = tool.get("function")
            if isinstance(function, dict):
                converted.append(
                    {
                        "type": "function",
                        "name": function.get("name"),  # ty:ignore[invalid-argument-type]
                        "description": function.get("description", ""),  # ty:ignore[no-matching-overload]
                        "parameters": function.get("parameters", {}),  # ty:ignore[no-matching-overload]
                        "strict": None,
                    }
                )
                continue
        converted.append(tool)
    return converted


def count_message_images(messages: list[Message]) -> int:
    count = 0
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                count += 1
    return count


def log_provider_event(
    logs_dir: Path, provider_name: str, event: str, **fields: object
) -> None:
    try:
        resolved_logs_dir = logs_dir.expanduser()
        resolved_logs_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        log_path = resolved_logs_dir / f"{provider_name}-{now:%Y-%m-%d}.jsonl"
        payload = {
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "provider": provider_name,
            "event": event,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            **sanitize_log_fields(fields),
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    except Exception:
        return


def sanitize_log_fields(fields: dict[str, object]) -> dict[str, object]:
    return {
        key: sanitize_log_value(value)
        for key, value in fields.items()
        if value is not None
    }


def sanitize_log_value(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= 300 else f"{value[:297]}..."
    if isinstance(value, int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return sanitize_log_fields(value)  # ty: ignore[invalid-argument-type]
    if isinstance(value, list | tuple):
        return [sanitize_log_value(item) for item in value[:20]]
    return str(value)


def exception_summary(exc: Exception) -> str:
    message = str(exc).strip()
    if len(message) > 220:
        message = f"{message[:217]}..."
    if not message:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"


def consume_sse_response(
    response: httpx.Response,
    *,
    provider_name: str | None = None,
    model_id: str | None = None,
) -> Message:
    text_parts: list[str] = []
    function_calls: dict[str, dict[str, str]] = {}
    completed_payload: dict[str, Any] | None = None
    usage_payload: object | None = None
    event_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            completed_payload, usage_payload = handle_sse_event(
                event_lines,
                text_parts,
                function_calls,
                completed_payload,
                usage_payload,
            )
            event_lines = []
            continue
        event_lines.append(line)
    if event_lines:
        completed_payload, usage_payload = handle_sse_event(
            event_lines,
            text_parts,
            function_calls,
            completed_payload,
            usage_payload,
        )
    if completed_payload is not None:
        merge_completed_response(completed_payload, text_parts, function_calls)
        usage_payload = completed_payload.get("usage") or usage_payload
    phase = message_phase_from_completed_response(completed_payload)
    tool_calls = [
        ToolCall(
            id=item.get("call_id") or item_id,
            function=ToolFunction(
                name=item.get("name") or "",
                arguments=item.get("arguments") or "{}",
            ),
        )
        for item_id, item in function_calls.items()
        if item.get("name")
    ]
    return Message(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        phase=phase,
        usage=parse_token_usage(
            usage_payload,
            provider_name=provider_name,
            model_id=model_id,
        ),
    )


def handle_sse_event(
    lines: list[str],
    text_parts: list[str],
    function_calls: dict[str, dict[str, str]],
    completed_payload: dict[str, Any] | None,
    usage_payload: object | None,
) -> tuple[dict[str, Any] | None, object | None]:
    data_lines = [line[5:].strip() for line in lines if line.startswith("data:")]
    if not data_lines:
        return completed_payload, usage_payload
    raw_data = "\n".join(data_lines)
    if raw_data == "[DONE]":
        return completed_payload, usage_payload
    try:
        event = json.loads(raw_data)
    except json.JSONDecodeError:
        return completed_payload, usage_payload
    event_type = event.get("type")
    if isinstance(event.get("usage"), dict):
        usage_payload = event.get("usage")
    if event_type in {"error", "response.failed"}:
        error_payload = event.get("error") if isinstance(event, dict) else None
        error_type = ""
        error_code = ""
        error_message = ""
        if isinstance(error_payload, dict):
            error_type = str(error_payload.get("type") or "").lower()
            error_code = str(error_payload.get("code") or "").lower()
            error_message = str(error_payload.get("message") or "").lower()
        haystack = f"{error_type} {error_code} {error_message}"
        transient_markers = (
            "server_error",
            "service_unavailable",
            "internal_error",
            "overloaded",
            "server_is_overloaded",
            "timeout",
            "bad_gateway",
            "gateway_timeout",
            "temporarily unavailable",
            "currently overloaded",
            "try again later",
        )
        if any(marker in haystack for marker in transient_markers):
            raise ProviderServerError(
                f"Codex stream failed: {event}",
                status_code=503,
            )
        if "rate_limit" in haystack:
            raise ProviderRateLimitError(f"Codex stream rate limited: {event}")
        raise ProviderError(f"Codex stream failed: {event}")
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str):
            text_parts.append(delta)
    elif event_type == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or event.get("output_index") or "")
        if item_id:
            item = function_calls.setdefault(item_id, {})
            item["arguments"] = item.get("arguments", "") + str(
                event.get("delta") or ""
            )
    elif event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            item_id = str(item.get("id") or item.get("call_id") or len(function_calls))
            stored = function_calls.setdefault(item_id, {})
            stored["call_id"] = str(item.get("call_id") or item_id)
            stored["name"] = str(item.get("name") or "")
            stored["arguments"] = str(
                item.get("arguments") or stored.get("arguments") or "{}"
            )
    elif event_type in {"response.completed", "response.done"}:
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            usage_payload = response_payload.get("usage") or usage_payload
            return response_payload, usage_payload
    return completed_payload, usage_payload


def merge_completed_response(
    payload: dict[str, Any],
    text_parts: list[str],
    function_calls: dict[str, dict[str, str]],
) -> None:
    output = payload.get("output")
    if not isinstance(output, list):
        return
    if text_parts:
        existing_text = "".join(text_parts)
    else:
        existing_text = ""
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":  # ty:ignore[invalid-argument-type]
            for content in item.get("content") or []:  # ty:ignore[invalid-argument-type]
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str) and text not in existing_text:
                        text_parts.append(text)
        if item.get("type") == "function_call":  # ty:ignore[invalid-argument-type]
            item_id = str(item.get("id") or item.get("call_id") or index)  # ty:ignore[invalid-argument-type]
            stored = function_calls.setdefault(item_id, {})
            stored["call_id"] = str(item.get("call_id") or item_id)  # ty:ignore[invalid-argument-type]
            stored["name"] = str(item.get("name") or "")  # ty:ignore[invalid-argument-type]
            stored["arguments"] = str(
                item.get("arguments") or stored.get("arguments") or "{}"  # ty:ignore[invalid-argument-type]
            )


def message_phase_from_completed_response(
    payload: dict[str, Any] | None,
) -> MessagePhase | None:
    if payload is None:
        return None
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    seen_commentary = False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        phase = normalize_message_phase(item.get("phase"))
        if phase == "final_answer":
            return phase
        if phase == "commentary":
            seen_commentary = True
    return "commentary" if seen_commentary else None


def normalize_message_phase(value: object) -> MessagePhase | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"commentary", "preamble"}:
        return "commentary"
    if normalized in {"final_answer", "final"}:
        return "final_answer"
    return None


def clamp_reasoning_effort(model: str, effort: str) -> str:
    normalized = effort.strip().lower()
    allowed = ("minimal", "low", "medium", "high", "xhigh")
    if normalized not in allowed:
        normalized = "medium"
    if normalized == "xhigh" and "gpt-5" not in model:
        return "high"
    return normalized


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def error_detail(response: httpx.Response) -> str:
    try:
        response.read()
    except httpx.ResponseNotRead:
        pass
    except httpx.CloseError:
        return f"HTTP {response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return response.text.strip() or f"HTTP {response.status_code}"


def is_invalid_oauth_token_error(detail: object) -> bool:
    normalized = str(detail).strip().lower()
    return (
        "invalidated oauth token" in normalized
        or "invalid oauth token" in normalized
        or ("oauth token" in normalized and "invalid" in normalized)
        or ("oauth token" in normalized and "revoked" in normalized)
    )


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None
