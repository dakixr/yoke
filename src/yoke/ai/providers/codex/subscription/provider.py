"""Codex subscription HTTP provider and model selection."""

from __future__ import annotations

import contextlib
import secrets
import time
from collections.abc import Callable

import httpx

from yoke._tls import tls_verification_enabled
from yoke.agent.models import Message
from yoke.ai.providers.base import (
    BaseProvider,
    ProviderCancelledError,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
)
from yoke.ai.providers.codex.cache import build_prompt_cache_key
from yoke.ai.providers.http_retry import (
    RetryDecision,
    StreamProgress,
    backoff_seconds,
    error_detail,
    response_error,
    run_with_retry,
)
from yoke.ai.providers.model_selection import (
    ModelCatalogMixin,
    set_config_model_from_catalog,
)

from .catalog import (
    LEGACY_MODEL_CATALOG,
    MODEL_CATALOG,
    OAUTH_PROVIDER_ID,
    PROVIDER_NAME,
)
from .config import CodexSubscriptionConfig
from .helpers import is_invalid_oauth_token_error
from .logging import exception_summary, log_provider_event
from .messages import count_message_images
from .models import OAuthCredentials
from .oauth import login_openai_codex, refresh_openai_codex_token
from .profiles import AuthStorage, CodexProfileStore
from .request import hosted_image_payload, request_headers, request_payload
from .sse import consume_hosted_image_sse_response, consume_sse_response


class CodexSubscriptionProvider[ConfigT: CodexSubscriptionConfig](
    ModelCatalogMixin[ConfigT],
    BaseProvider[ConfigT, httpx.Client],
):
    _active_auth_profile: str | None
    _last_logged_auth_profile: str | None
    _turn_state: str | None
    provider_name = PROVIDER_NAME
    supports_image_inputs = True
    max_images_per_message = None
    supports_image_generation = True

    def __init__(
        self,
        config: ConfigT,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        set_config_model_from_catalog(
            self.config,
            MODEL_CATALOG + LEGACY_MODEL_CATALOG,
            provider_name=PROVIDER_NAME,
            model_id=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
        )
        self._sleep = sleep or time.sleep
        self._active_auth_profile: str | None = None
        self._last_logged_auth_profile: str | None = None
        self._prompt_cache_key = self._new_prompt_cache_key()
        self._turn_state: str | None = None
        self._owns_client = http_client is None
        self._client = http_client or self._new_client()

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.config.timeout_seconds,
            verify=tls_verification_enabled(),
        )

    def _fork_client(self) -> httpx.Client | None:
        return None if self._owns_client else self._client

    def list_models(self) -> list[ProviderModelInfo]:
        return [model.model_copy(deep=True) for model in MODEL_CATALOG]

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
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> dict[str, object]:
        return request_payload(
            messages,
            tools,
            config=self.config,
            prompt_cache_key=self._prompt_cache_key,
            turn_state=self._turn_state,
        )

    def _request_headers(self, credentials: OAuthCredentials) -> dict[str, str]:
        return request_headers(
            credentials,
            config=self.config,
            prompt_cache_key=self._prompt_cache_key,
            turn_state=self._turn_state,
        )

    def _responses_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/codex/responses"):
            return base_url
        if base_url.endswith("/codex"):
            return f"{base_url}/responses"
        return f"{base_url}/codex/responses"

    def _backoff_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        return backoff_seconds(
            attempt,
            self.config.retry_backoff_seconds,
            self.config.max_retry_backoff_seconds,
            retry_after,
        )

    def _clear_selection_cache(self, *, reason: str = "rate_limit") -> None:
        """Clear the cached profile selection to force account rotation on the next credential fetch."""
        self._turn_state = None
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
            status_code=exc.status_code if isinstance(exc, ProviderError) else None,
            auth_profile=auth_profile,
            **request_metrics,
        )

    def _log_event(self, event: str, **fields: object) -> None:
        log_provider_event(self.config.logs_dir, self.provider_name, event, **fields)

    def generate_image(self, *, prompt: str) -> str:
        """Generate an image through Codex's hosted Responses image tool."""
        return self._generate_hosted_image(
            prompt=prompt,
            reference_image_urls=[],
        )

    def edit_image(self, *, prompt: str, image_urls: list[str]) -> str:
        """Generate an edited image using reference image data URLs."""
        if not image_urls:
            raise ProviderError(
                "Codex image edit requires at least one reference image."
            )
        return self._generate_hosted_image(
            prompt=prompt,
            reference_image_urls=image_urls,
        )

    def _generate_hosted_image(
        self, *, prompt: str, reference_image_urls: list[str]
    ) -> str:
        credentials = self._fresh_credentials()
        payload = self._hosted_image_payload(
            prompt=prompt,
            reference_image_urls=reference_image_urls,
        )
        with self._client.stream(
            "POST",
            self._responses_url(),
            json=payload,
            headers=self._request_headers(credentials),
        ) as response:
            if response.is_error:
                operation = "edit" if reference_image_urls else "generation"
                raise ProviderError(
                    f"Codex image {operation} failed: {error_detail(response)}",
                    status_code=response.status_code,
                )
            return consume_hosted_image_sse_response(response)

    def _hosted_image_payload(
        self, *, prompt: str, reference_image_urls: list[str]
    ) -> dict[str, object]:
        return hosted_image_payload(
            prompt=prompt,
            reference_image_urls=reference_image_urls,
            config=self.config,
            prompt_cache_key=self._prompt_cache_key,
        )

    def _complete_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        request_started = time.monotonic()
        request_id = secrets.token_hex(8)
        metrics = self._request_log_metrics(messages, tools)
        credentials = self._fresh_credentials()
        auth_profile = self._active_auth_profile
        payload = self._request_payload(messages, tools)
        headers = self._request_headers(credentials)
        progress: StreamProgress | None = None
        http_status: int | None = None

        def log_retry(
            number: int,
            reason: str,
            delay: float,
            retry_after: float | None,
            status: int | None = None,
        ) -> None:
            fields: dict[str, object] = dict(
                request_id=request_id,
                attempt=number,
                wait_seconds=delay,
                retry_after_used=retry_after is not None,
                reason=reason,
                auth_profile=auth_profile,
                **metrics,
            )
            if status is not None:
                fields["status_code"] = status
            self._log_event("request_retry", **fields)

        def attempt(number: int) -> Message:
            nonlocal progress, http_status
            progress = None
            http_status = None
            with self._client.stream(
                "POST", self._responses_url(), json=payload, headers=headers
            ) as response:
                if response.is_error:
                    http_status = response.status_code
                    error = response_error(response, prefix="Codex")
                    assert error is not None
                    raise error
                progress = StreamProgress(response)
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
                    request_id=request_id,
                    attempt=number,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    tool_call_count=len(message.tool_calls or []),
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                    auth_profile=auth_profile,
                    **metrics,
                )
                return message

        def classify(error: Exception, number: int) -> RetryDecision:
            nonlocal credentials, auth_profile, headers
            delay = 0.0
            if number < self.config.max_retries and isinstance(
                error,
                ProviderRateLimitError | ProviderServerError | httpx.TimeoutException,
            ):
                retry_after = (
                    error.retry_after_seconds
                    if isinstance(error, ProviderRateLimitError)
                    else None
                )
                delay = self._backoff_seconds(number, retry_after)

            def log_cancelled() -> None:
                self._log_event(
                    "request_cancelled",
                    request_id=request_id,
                    attempt=number,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    auth_profile=auth_profile,
                    **metrics,
                )

            if isinstance(error, ProviderRateLimitError):

                def rate_limit() -> None:
                    nonlocal credentials, auth_profile, headers
                    self._clear_selection_cache()
                    credentials = self._fresh_credentials()
                    auth_profile = self._active_auth_profile
                    headers = self._request_headers(credentials)
                    log_retry(
                        number,
                        "rate_limited"
                        if http_status is not None
                        else "stream_rate_limited",
                        delay,
                        error.retry_after_seconds,
                        http_status,
                    )

                decision = RetryDecision(
                    error,
                    delay,
                    before_retry=rate_limit,
                    on_backoff_cancel=log_cancelled
                    if http_status is not None
                    else None,
                )
            elif isinstance(error, ProviderServerError):
                # Preserve the historic terminal HTTP failure log emitted by
                # the stream-error handler once the HTTP budget is exhausted.
                reason = (
                    "server_error" if http_status is not None else "stream_server_error"
                )
                decision = RetryDecision(
                    error,
                    delay,
                    before_retry=lambda: log_retry(
                        number, reason, delay, None, error.status_code
                    ),
                    on_backoff_cancel=log_cancelled
                    if http_status is not None
                    else None,
                )
            elif isinstance(error, httpx.TimeoutException):
                if cancel_requested():
                    return RetryDecision(ProviderCancelledError())
                if progress is not None and progress.started:
                    return RetryDecision(
                        ProviderError(
                            "Codex stream interrupted after response bytes were received."
                        )
                    )
                mapped = ProviderError("Codex request timed out.")
                decision = RetryDecision(
                    mapped,
                    delay,
                    before_retry=lambda: log_retry(number, "timeout", delay, None),
                )
            elif isinstance(error, ProviderCancelledError):
                self._log_event(
                    "request_cancelled",
                    request_id=request_id,
                    attempt=number,
                    duration_seconds=round(time.monotonic() - request_started, 3),
                    auth_profile=auth_profile,
                    **metrics,
                )
                return RetryDecision(error)
            elif isinstance(error, httpx.RequestError):
                if cancel_requested():
                    return RetryDecision(ProviderCancelledError())
                mapped = ProviderError(f"Codex request failed: {error}")
                self._log_request_failure(
                    request_id, request_started, number, mapped, auth_profile, metrics
                )
                return RetryDecision(mapped)
            elif isinstance(error, ProviderError) and http_status is not None:
                detail = str(error).removeprefix("Codex request failed: ")
                if is_invalid_oauth_token_error(detail):
                    recovered = self._recover_invalid_oauth_credentials(
                        auth_profile=auth_profile,
                        request_id=request_id,
                        attempt=number,
                        detail=detail,
                        request_metrics=metrics,
                    )
                    if recovered is not None and number < self.config.max_retries:
                        credentials = recovered
                        auth_profile = self._active_auth_profile
                        headers = self._request_headers(credentials)
                        return RetryDecision(
                            error,
                            0.0,
                            recover=True,
                            before_retry=lambda: log_retry(
                                number, "invalid_oauth_token", 0.0, None, http_status
                            ),
                        )
                return RetryDecision(error)
            else:
                return RetryDecision(error)
            if number >= self.config.max_retries:
                self._log_request_failure(
                    request_id,
                    request_started,
                    number,
                    decision.error,
                    auth_profile,
                    metrics,
                )
            return decision

        return run_with_retry(
            attempt,
            classify,
            max_retries=self.config.max_retries,
            cancel_requested=cancel_requested,
            sleep=self._sleep,
            check_cancel=False,
        )

    def _set_turn_state(self, value: str) -> None:
        self._turn_state = value

    def start_turn(self) -> None:
        """Clear Codex routing metadata at a logical user-turn boundary."""
        self._turn_state = None

    def _new_prompt_cache_key(self) -> str:
        return build_prompt_cache_key(self.config)

    def set_session_id(self, session_id: str) -> None:
        self.config.prompt_cache_key = session_id
        self._prompt_cache_key = self._new_prompt_cache_key()
        self._turn_state = None
