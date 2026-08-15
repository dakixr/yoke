"""Native Z.AI provider transport."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    Provider,
    ProviderCancelledError,
    ProviderError,
    ProviderModelInfo,
    ProviderRateLimitError,
    ProviderServerError,
    sleep_with_cancel,
)
from yoke.ai.providers.model_selection import set_config_model_from_catalog
from yoke.ai.providers.zai.models import (
    GLM_52_THINKING_LEVELS,
    GLM_53_THINKING_LEVELS,
    MODEL_CATALOG,
    PROVIDER_NAME,
    THINKING_LEVELS,
    ZAIConfig,
    _thinking_config,
)
from yoke.ai.providers.zai.recovery import ZAIMessageRecoveryMixin
from yoke.ai.providers.zai.retry import ZAIRetryMixin
from yoke.ai.providers.zai.streaming import ZAIStreamingMixin


class ZAIProvider(
    ZAIStreamingMixin,
    ZAIMessageRecoveryMixin,
    ZAIRetryMixin,
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
        return ProviderModelInfo(
            id=current_model,
            display_name=current_model,
            context_window_tokens=128_000,
            thinking_levels=(
                GLM_53_THINKING_LEVELS
                if current_model == "glm-5.3"
                else GLM_52_THINKING_LEVELS
                if current_model == "glm-5.2"
                else THINKING_LEVELS
            ),
            default_thinking_level=(
                "max" if current_model in {"glm-5.2", "glm-5.3"} else "thinking"
            ),
            supports_image_inputs=False,
        )

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
        prepared_messages = self._prepare_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                self._message_to_api_dict(message) for message in prepared_messages
            ],
            "stream": True,
        }
        thinking = _thinking_config(self.config.model, self.config.reasoning_effort)
        if thinking is not None:
            payload["thinking"] = thinking
        if self.config.model in {"glm-5.2", "glm-5.3"}:
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
            try:
                with self._client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    timeout=stream_timeout,
                ) as response:
                    if response.status_code == 429:
                        response.read()
                        detail = self._error_detail(response)
                        retry_after = self._retry_after_seconds(response)
                        last_error = ProviderRateLimitError(
                            (
                                f"ZAI request failed after {attempt + 1} attempts: {detail}"
                                if attempt >= self.config.max_retries
                                else f"ZAI request was rate limited: {detail}"
                            ),
                            retry_after_seconds=retry_after,
                        )
                        if attempt < self.config.max_retries:
                            sleep_with_cancel(
                                retry_after or self._backoff_seconds(attempt),
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error

                    if 500 <= response.status_code < 600:
                        response.read()
                        detail = self._error_detail(response)
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
                                self._backoff_seconds(attempt),
                                cancel_requested=cancel_requested,
                                sleep=self._sleep,
                            )
                            continue
                        raise last_error

                    if response.is_error:
                        response.read()
                        detail = self._error_detail(response)
                        if (
                            not attempted_message_recovery
                            and self._looks_like_illegal_messages_error(detail)
                        ):
                            self._log_debug_event(
                                "illegal_messages_error",
                                detail=detail,
                                messages=[
                                    message.to_api_dict()
                                    for message in prepared_messages
                                ],
                            )
                            recovered_messages = self._recover_illegal_messages(
                                prepared_messages
                            )
                            attempted_message_recovery = True
                            if recovered_messages != prepared_messages:
                                self._log_debug_event(
                                    "illegal_messages_recovery",
                                    detail=detail,
                                    messages=[
                                        message.to_api_dict()
                                        for message in recovered_messages
                                    ],
                                )
                                prepared_messages = recovered_messages
                                payload["messages"] = [
                                    self._message_to_api_dict(message)
                                    for message in prepared_messages
                                ]
                                continue
                        raise ProviderError(
                            f"ZAI request failed: {detail}",
                            status_code=response.status_code,
                        )

                    message = self._parse_sse_response(
                        response,
                        cancel_requested=cancel_requested,
                    )
                    return message
            except httpx.TimeoutException as exc:
                last_error = ProviderError(
                    (
                        f"ZAI request timed out after {attempt + 1} attempts."
                        if attempt >= self.config.max_retries
                        else "ZAI request timed out (server went silent)."
                    )
                )
                if attempt < self.config.max_retries:
                    sleep_with_cancel(
                        self._backoff_seconds(attempt),
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
        finished = threading.Event()
        client_closed = threading.Event()

        def close_on_cancel() -> None:
            while not finished.wait(0.05):
                if cancel_requested():
                    client_closed.set()
                    self._client.close()
                    return

        threading.Thread(target=close_on_cancel, daemon=True).start()
        try:
            message = action()
            if cancel_requested():
                raise ProviderCancelledError()
            return message
        finally:
            finished.set()
            if client_closed.is_set():
                self._client = self._new_client()

    def close(self) -> None:
        """Close the owned HTTP client, if this provider created it."""

        if self._owns_client:
            self._client.close()
