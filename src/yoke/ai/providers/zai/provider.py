"""Native Z.AI provider transport."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from yoke._tls import tls_verification_enabled
from yoke.agent.models import Message
from yoke.ai.providers.cancellation import cancellation_watcher
from yoke.ai.providers.base import (
    Provider,
    ProviderCancelledError,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
    sleep_with_cancel,
)
from yoke.ai.providers.model_selection import ModelCatalogMixin
from yoke.ai.providers.model_selection import set_config_model_from_catalog
from yoke.ai.providers.zai.models import (
    MODEL_CATALOG,
    PROVIDER_NAME,
    ZAIConfig,
    _thinking_config,
)
from yoke.ai.providers.zai.recovery import ZAIMessageRecovery
from yoke.ai.providers.http_retry import (
    backoff_seconds,
    error_detail,
    retry_after_seconds,
    retryable_status,
    StreamProgress,
)
from yoke.ai.providers.zai.streaming import parse_sse_response


class ZAIProvider(
    ModelCatalogMixin[ZAIConfig],
    Provider,
):
    """Provider for Z.AI's coding chat-completions API."""

    provider_name = PROVIDER_NAME
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(
        self,
        config: ZAIConfig,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        set_config_model_from_catalog(
            self.config,
            MODEL_CATALOG,
            provider_name=PROVIDER_NAME,
            model_id=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
        )
        self._recovery = ZAIMessageRecovery(self.config)
        self._owns_client = http_client is None
        self._sleep = sleep or time.sleep
        self._client = http_client or self._new_client()

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.base_url.rstrip("/"),
            timeout=httpx.Timeout(
                self.config.total_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
            ),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
            verify=tls_verification_enabled(),
        )

    def fork_for_turn(self) -> ZAIProvider:
        """Copy request state, retaining a borrowed transport without owning it."""
        forked = type(self)(
            self.config.model_copy(deep=True),
            http_client=None if self._owns_client else self._client,
            sleep=self._sleep,
        )
        return forked

    def list_models(self) -> list[ProviderModelInfo]:
        return [model.model_copy(deep=True) for model in MODEL_CATALOG]

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        set_config_model_from_catalog(
            self.config,
            MODEL_CATALOG,
            provider_name=PROVIDER_NAME,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Send one request to Z.AI and return the first completion message."""
        return self._complete_impl(messages, tools, cancel_requested=lambda: False)

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        return self._with_request_cancellation(
            lambda: self._complete_impl(
                messages,
                tools,
                cancel_requested=cancel_requested,
            ),
            cancel_requested=cancel_requested,
        )

    def _complete_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        prepared_messages = self._recovery._prepare_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                self._recovery._message_to_api_dict(message)
                for message in prepared_messages
            ],
            "stream": True,
        }
        thinking = _thinking_config(self.config.model, self.config.reasoning_effort)
        if thinking is not None:
            payload["thinking"] = thinking
        payload["reasoning_effort"] = self.config.reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        stream_timeout = httpx.Timeout(
            self.config.read_idle_timeout_seconds,
            connect=self.config.connect_timeout_seconds,
        )
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"

        last_error: ProviderError | None = None
        attempted_message_recovery = False

        # Message-shape recovery gets one resend even when the transport retry
        # budget is zero; normal network/server retries retain their exact cap.
        for attempt in range(self.config.max_retries + 2):
            if cancel_requested():
                raise ProviderCancelledError()
            progress: StreamProgress | None = None
            try:
                with self._client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    timeout=stream_timeout,
                ) as response:
                    if response.status_code == 429:
                        response.read()
                        detail = error_detail(response)
                        retry_after = retry_after_seconds(response)
                        last_error = ProviderRateLimitError(
                            (
                                f"ZAI request failed after {attempt + 1} attempts: {detail}"
                                if attempt >= self.config.max_retries
                                else f"ZAI request was rate limited: {detail}"
                            ),
                            retry_after_seconds=retry_after,
                        )
                        if attempt < self.config.max_retries:
                            # ZAI honors the server's full Retry-After value.
                            sleep_with_cancel(
                                backoff_seconds(
                                    attempt,
                                    self.config.retry_backoff_seconds,
                                    self.config.max_retry_backoff_seconds,
                                )
                                if retry_after is None
                                else retry_after,
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error

                    if retryable_status(response.status_code):
                        response.read()
                        detail = error_detail(response)
                        last_error = ProviderServerError(
                            (
                                f"ZAI request failed after {attempt + 1} attempts: {detail}"
                                if attempt >= self.config.max_retries
                                else f"ZAI server error: {detail}"
                            ),
                            status_code=response.status_code,
                        )
                        if attempt < self.config.max_retries:
                            sleep_with_cancel(
                                backoff_seconds(
                                    attempt,
                                    self.config.retry_backoff_seconds,
                                    self.config.max_retry_backoff_seconds,
                                ),
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error

                    if response.is_error:
                        response.read()
                        detail = error_detail(response)
                        if (
                            not attempted_message_recovery
                            and self._recovery._looks_like_illegal_messages_error(
                                detail
                            )
                        ):
                            self._recovery._log_debug_event(
                                "illegal_messages_error",
                                detail=detail,
                                messages=[
                                    message.to_api_dict()
                                    for message in prepared_messages
                                ],
                            )
                            recovered_messages = (
                                self._recovery._recover_illegal_messages(
                                    prepared_messages
                                )
                            )
                            attempted_message_recovery = True
                            if recovered_messages != prepared_messages:
                                self._recovery._log_debug_event(
                                    "illegal_messages_recovery",
                                    detail=detail,
                                    messages=[
                                        message.to_api_dict()
                                        for message in recovered_messages
                                    ],
                                )
                                prepared_messages = recovered_messages
                                payload["messages"] = [
                                    self._recovery._message_to_api_dict(message)
                                    for message in prepared_messages
                                ]
                                continue
                        raise ProviderError(
                            f"ZAI request failed: {detail}",
                            status_code=response.status_code,
                        )

                    progress = StreamProgress(response)
                    message = parse_sse_response(
                        response,
                        model_id=self.config.model,
                        cancel_requested=cancel_requested,
                    )
                    return message
            except httpx.TimeoutException as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                if progress is not None and progress.started:
                    raise ProviderError(
                        "ZAI stream interrupted after response bytes were received."
                    ) from exc
                last_error = ProviderError(
                    (
                        f"ZAI request timed out after {attempt + 1} attempts."
                        if attempt >= self.config.max_retries
                        else "ZAI request timed out (server went silent)."
                    )
                )
                if attempt < self.config.max_retries:
                    sleep_with_cancel(
                        backoff_seconds(
                            attempt,
                            self.config.retry_backoff_seconds,
                            self.config.max_retry_backoff_seconds,
                        ),
                        cancel_requested=cancel_requested,
                        sleep=self._sleep,
                    )
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                raise ProviderError(f"ZAI request failed: {exc}") from exc

        if last_error is not None:
            raise last_error
        raise ProviderError("ZAI request failed unexpectedly.")

    def _with_request_cancellation(
        self,
        action: Callable[[], Message],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        if not self._owns_client:
            return action()

        def rebuild() -> None:
            self._client = self._new_client()

        with cancellation_watcher(
            cancel_requested,
            lambda: self._client.close(),
            rebuild=rebuild,
        ):
            return action()

    def close(self) -> None:
        """Close the owned HTTP client, if this provider created it."""

        if self._owns_client:
            self._client.close()
