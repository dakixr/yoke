"""Native Z.AI provider transport."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

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
from yoke.ai.providers.http_retry import (
    RetryDecision,
    StreamProgress,
    backoff_seconds,
    error_detail,
    retry_after_seconds,
    retryable_status,
    run_with_retry,
)
from yoke.ai.providers.model_selection import (
    ModelCatalogMixin,
    set_config_model_from_catalog,
)
from yoke.ai.providers.zai.models import (
    MODEL_CATALOG,
    PROVIDER_NAME,
    ZAIConfig,
    _thinking_config,
)
from yoke.ai.providers.zai.recovery import ZAIMessageRecovery
from yoke.ai.providers.zai.streaming import parse_sse_response


class ZAIProvider(
    ModelCatalogMixin[ZAIConfig],
    BaseProvider[ZAIConfig, httpx.Client],
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

    def _fork_client(self) -> httpx.Client | None:
        return None if self._owns_client else self._client

    def list_models(self) -> list[ProviderModelInfo]:
        return [model.model_copy(deep=True) for model in MODEL_CATALOG]

    def _complete_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        prepared_messages = self._recovery.prepare_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                self._recovery.message_to_api_dict(message)
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

        attempted_message_recovery = False
        progress: StreamProgress | None = None

        def attempt(number: int) -> Message:
            nonlocal progress
            progress = None
            with self._client.stream(
                "POST", endpoint, json=payload, timeout=stream_timeout
            ) as response:
                if response.is_error:
                    detail = error_detail(response)
                    if response.status_code == 429:
                        raise ProviderRateLimitError(
                            f"ZAI request failed after {number + 1} attempts: {detail}"
                            if number >= self.config.max_retries
                            else f"ZAI request was rate limited: {detail}",
                            retry_after_seconds=retry_after_seconds(response),
                        )
                    if retryable_status(response.status_code):
                        raise ProviderServerError(
                            f"ZAI request failed after {number + 1} attempts: {detail}"
                            if number >= self.config.max_retries
                            else f"ZAI server error: {detail}",
                            status_code=response.status_code,
                        )
                    raise ProviderError(
                        f"ZAI request failed: {detail}",
                        status_code=response.status_code,
                    )
                progress = StreamProgress(response)
                return parse_sse_response(
                    response,
                    model_id=self.config.model,
                    cancel_requested=cancel_requested,
                )

        def classify(error: Exception, number: int) -> RetryDecision:
            nonlocal attempted_message_recovery, prepared_messages
            delay = 0.0
            if number < self.config.max_retries:
                if (
                    isinstance(error, ProviderRateLimitError)
                    and error.retry_after_seconds is not None
                ):
                    delay = error.retry_after_seconds
                elif isinstance(
                    error,
                    ProviderRateLimitError
                    | ProviderServerError
                    | httpx.TimeoutException,
                ):
                    delay = backoff_seconds(
                        number,
                        self.config.retry_backoff_seconds,
                        self.config.max_retry_backoff_seconds,
                    )
            if isinstance(error, httpx.TimeoutException):
                if cancel_requested():
                    return RetryDecision(ProviderCancelledError())
                if progress is not None and progress.started:
                    return RetryDecision(
                        ProviderError(
                            "ZAI stream interrupted after response bytes were received."
                        )
                    )
                return RetryDecision(
                    ProviderError(
                        f"ZAI request timed out after {number + 1} attempts."
                        if number >= self.config.max_retries
                        else "ZAI request timed out (server went silent)."
                    ),
                    delay,
                )
            if isinstance(error, httpx.RequestError):
                return RetryDecision(
                    ProviderCancelledError()
                    if cancel_requested()
                    else ProviderError(f"ZAI request failed: {error}")
                )
            if isinstance(error, ProviderRateLimitError):
                return RetryDecision(
                    error,
                    error.retry_after_seconds
                    if error.retry_after_seconds is not None
                    else delay,
                )
            if isinstance(error, ProviderServerError):
                return RetryDecision(error, delay)
            if isinstance(error, ProviderError) and error.status_code is not None:
                detail = str(error).removeprefix("ZAI request failed: ")
                if (
                    not attempted_message_recovery
                    and self._recovery.looks_like_illegal_messages_error(detail)
                ):
                    self._recovery.log_debug_event(
                        "illegal_messages_error",
                        detail=detail,
                        messages=[
                            message.to_api_dict() for message in prepared_messages
                        ],
                    )
                    recovered = self._recovery.recover_illegal_messages(
                        prepared_messages
                    )
                    attempted_message_recovery = True
                    if recovered != prepared_messages:

                        def recover() -> None:
                            nonlocal prepared_messages
                            self._recovery.log_debug_event(
                                "illegal_messages_recovery",
                                detail=detail,
                                messages=[
                                    message.to_api_dict() for message in recovered
                                ],
                            )
                            prepared_messages = recovered
                            payload["messages"] = [
                                self._recovery.message_to_api_dict(message)
                                for message in recovered
                            ]

                        return RetryDecision(
                            error, 0.0, recover=True, before_retry=recover
                        )
            return RetryDecision(error)

        return run_with_retry(
            attempt,
            classify,
            max_retries=self.config.max_retries,
            max_recoveries=1,
            cancel_requested=cancel_requested,
            sleep=self._sleep,
        )
