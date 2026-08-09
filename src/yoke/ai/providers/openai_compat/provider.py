"""openai_compat provider module."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

from yoke.agent.message_sanitizer import sanitize_json_surrogates
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import cloned_model_catalog
from yoke.ai.providers.model_selection import (
    current_model_id_from_config,
)
from yoke.ai.providers.model_selection import (
    current_model_info_from_catalog,
)
from yoke.ai.providers.model_selection import (
    set_config_model_from_catalog,
)
from yoke.ai.providers.openai_compat.content import (
    normalize_openai_request_messages,
)
from yoke.ai.providers.openai_compat.content import (
    serialize_message_for_openai,
)
from yoke.ai.providers.openai_compat.client import LazyHttpClient
from yoke.ai.providers.openai_compat.events import (
    emit_recovery_event,
)
from yoke.ai.providers.openai_compat.helpers import (
    thinking_levels_for_reasoning_effort,
)
from yoke.ai.providers.openai_compat.models import (
    OpenAICompatibleChatCompletionResponse,
)
from yoke.ai.providers.openai_compat.retry import OpenAICompatibleRetryMixin
from yoke.ai.providers.usage import parse_token_usage

from . import models as _compat_models

OpenAICompatibleChoice = _compat_models.OpenAICompatibleChoice
OpenAICompatibleResponseMessage = _compat_models.OpenAICompatibleResponseMessage


class OpenAICompatibleConfig(BaseModel):
    """Configuration for providers exposing an OpenAI-compatible API."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    chat_completions_path: str = "/chat/completions"
    timeout_seconds: float | None = 120.0
    headers: dict[str, str] = Field(default_factory=dict)
    api_key_header_name: str = "Authorization"
    api_key_header_prefix: str = "Bearer "
    max_retries: int = 8
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 300.0
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    provider_name: str = "openai-compatible"
    model_catalog: tuple[ProviderModelInfo, ...] = Field(default_factory=tuple)

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str | None) -> str | None:
        """Validate normalized reasoning effort values when configured."""
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError(
                "reasoning_effort must be one of none, low, medium, high, xhigh or max"
            )
        return normalized

    @model_validator(mode="after")
    def validate_reasoning_effort_path(self) -> OpenAICompatibleConfig:
        """Require a chat-completions request shape for reasoning effort."""
        if self.reasoning_effort and self.chat_completions_path != "/chat/completions":
            raise ValueError(
                "reasoning_effort is only supported for /chat/completions "
                "requests in this provider"
            )
        return self

    @field_validator("api_key_header_name")
    @classmethod
    def validate_api_key_header_name(cls, value: str) -> str:
        """Validate the configured auth header name."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("api_key_header_name must not be empty")
        return normalized

    @classmethod
    def from_env(cls, **overrides: object) -> OpenAICompatibleConfig:
        """Create a config populated from standardized environment variables."""
        values: dict[str, Any] = {
            "api_key": os.getenv("YOKE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"),
            "model": os.getenv("YOKE_OPENAI_MODEL") or os.getenv("OPENAI_MODEL"),
            "base_url": os.getenv("YOKE_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL"),
        }
        values.update(
            {key: value for key, value in overrides.items() if value is not None}
        )
        return cls(**{key: value for key, value in values.items() if value is not None})


class OpenAICompatibleProvider(OpenAICompatibleRetryMixin, Provider):
    """Provider for generic OpenAI-compatible chat-completions endpoints.

    Use this when the upstream service accepts bearer authentication and the
    standard OpenAI `/chat/completions` request shape.
    """

    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.provider_name = config.provider_name
        if self.config.model_catalog:
            set_config_model_from_catalog(
                self.config,
                self.config.model_catalog,
                provider_name=self.provider_name,
                model_id=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
            )
        self._owns_client = http_client is None
        self._sleep = sleep or time.sleep
        self._headers = {
            config.api_key_header_name: (
                f"{config.api_key_header_prefix}{config.api_key}"
            ),
            "Content-Type": "application/json",
            **config.headers,
        }
        self._client = LazyHttpClient(
            http_client,
            lambda: httpx.Client(
                base_url=config.base_url.rstrip("/"),
                timeout=config.timeout_seconds,
                headers=self._headers,
            ),
        )

    def list_models(self) -> list[ProviderModelInfo]:
        """Return the configured model catalog for this provider."""
        if self.config.model_catalog:
            return cloned_model_catalog(self.config.model_catalog)
        return [
            ProviderModelInfo(
                id=self.config.model,
                display_name=self.config.model,
                context_window_tokens=400_000,
                thinking_levels=thinking_levels_for_reasoning_effort(
                    self.config.reasoning_effort
                ),
                supports_image_inputs=self.supports_image_inputs,
            )
        ]

    def current_model_id(self) -> str | None:
        """Return the currently configured model id."""
        return current_model_id_from_config(self.config)

    def current_model_info(self) -> ProviderModelInfo | None:
        """Return metadata for the current model when available."""
        model_info = current_model_info_from_catalog(self.config, self.list_models())
        if model_info is not None:
            return model_info
        current_model = self.current_model_id()
        if current_model is None:
            return None
        return ProviderModelInfo(
            id=current_model,
            display_name=current_model,
            context_window_tokens=400_000,
            thinking_levels=thinking_levels_for_reasoning_effort(
                self.config.reasoning_effort
            ),
            supports_image_inputs=self.supports_image_inputs,
        )

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        """Switch to a provider-advertised model and optional thinking level."""
        set_config_model_from_catalog(
            self.config,
            self.list_models(),
            provider_name=self.provider_name,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Send one request and return the first completion message."""
        return self._complete_impl(
            messages,
            tools,
            cancel_requested=lambda: False,
        )

    def _complete_impl(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        """Execute one completion with cooperative retry cancellation."""
        provider_messages = normalize_openai_request_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                serialize_message_for_openai(message) for message in provider_messages
            ],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
        payload = sanitize_json_surrogates(payload)
        last_error: ProviderError | None = None
        for attempt in range(self.config.max_retries + 1):
            if cancel_requested():
                raise ProviderCancelledError()
            try:
                response = self._client.post(
                    self._chat_completions_url(),
                    json=payload,
                    headers=self._headers,
                )
            except httpx.TimeoutException as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                last_error = ProviderError("Provider request timed out.")
                if isinstance(exc, httpx.ReadTimeout):
                    raise last_error from exc
                if attempt < self.config.max_retries:
                    self._retry_sleep(
                        last_error,
                        attempt=attempt,
                        cancel_requested=cancel_requested,
                    )
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                last_error = self._handle_request_error(
                    exc,
                    attempt=attempt,
                    cancel_requested=cancel_requested,
                )
                if last_error is not None:
                    continue
                raise ProviderError(f"Provider request failed: {exc}") from exc

            last_error = self._handle_error_response(
                response,
                attempt=attempt,
                cancel_requested=cancel_requested,
            )
            if last_error is not None:
                continue

            response_model = OpenAICompatibleChatCompletionResponse
            try:
                completion = response_model.model_validate(response.json())
            except (ValueError, ValidationError) as exc:
                raise ProviderError(
                    "Provider returned an invalid response payload."
                ) from exc

            if not completion.choices:
                raise ProviderError("Provider returned no completion choices.")
            message = completion.choices[0].message.to_message()
            message.usage = parse_token_usage(
                completion.usage,
                provider_name=self.provider_name,
                model_id=self.config.model,
            )
            emit_recovery_event(
                provider_name=self.provider_name,
                model_id=self.config.model,
                attempts=attempt,
            )
            return message

        if last_error is not None:
            raise last_error
        raise ProviderError("Provider request failed unexpectedly.")

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        """Complete a request while allowing an owned client to be aborted."""
        if not self._owns_client:
            return self._complete_impl(
                messages,
                tools,
                cancel_requested=cancel_requested,
            )

        finished = threading.Event()

        def abort_on_cancel() -> None:
            while not finished.wait(0.05):
                if cancel_requested():
                    self._client.abort()

        threading.Thread(target=abort_on_cancel, daemon=True).start()
        try:
            response = self._complete_impl(
                messages,
                tools,
                cancel_requested=cancel_requested,
            )
            if cancel_requested():
                raise ProviderCancelledError()
            return response
        except ProviderError as exc:
            if cancel_requested():
                raise ProviderCancelledError() from exc
            raise
        finally:
            finished.set()

    def close(self) -> None:
        """Close the owned HTTP client, if this provider created it."""
        if self._owns_client:
            self._client.close()

    def _chat_completions_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        path = self.config.chat_completions_path.lstrip("/")
        return f"{base_url}/{path}"
