"""openai_compat provider module."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from yoke._tls import tls_verification_enabled
from yoke.agent.message_sanitizer import sanitize_json_surrogates
from yoke.agent.models import Message
from yoke.ai.providers.base import (
    BaseProvider,
    ProviderCancelledError,
    ProviderError,
    ProviderModelInfo,
)
from yoke.ai.providers.cancellation import cancellation_watcher
from yoke.ai.providers.http_retry import HTTPRetryPolicy, response_error, run_with_retry
from yoke.ai.providers.model_selection import (
    ModelCatalogMixin,
    cloned_model_catalog,
    current_model_info_from_catalog,
    set_config_model_from_catalog,
)
from yoke.ai.providers.openai_compat.client import LazyHttpClient
from yoke.ai.providers.openai_compat.content import (
    normalize_openai_request_messages,
    serialize_message_for_openai,
)
from yoke.ai.providers.openai_compat.events import (
    emit_recovery_event,
)
from yoke.ai.providers.openai_compat.helpers import (
    thinking_levels_for_reasoning_effort,
)
from yoke.ai.providers.openai_compat.models import (
    OpenAICompatibleChatCompletionResponse,
)
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
    timeout_seconds: float | None = 900.0
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
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError(
                "reasoning_effort must be one of none, minimal, low, medium, "
                "high, xhigh or max"
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


class OpenAICompatibleProvider(
    ModelCatalogMixin[OpenAICompatibleConfig],
    BaseProvider[OpenAICompatibleConfig, LazyHttpClient],
):
    """Provider for generic OpenAI-compatible chat-completions endpoints.

    Use this when the upstream service accepts bearer authentication and the
    standard OpenAI `/chat/completions` request shape.
    """

    _timeout_as_request_error = False
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
                verify=tls_verification_enabled(),
            ),
        )

    def _fork_client(self) -> httpx.Client | None:
        return None if self._owns_client else self._client.get_client()

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

    def set_request_header(self, name: str, value: str) -> None:
        """Set a header on subsequent requests and owned client construction."""
        self.config.headers[name] = value
        self._headers[name] = value

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

        def attempt(number: int) -> Message:
            response = self._client.post(
                self._chat_completions_url(), json=payload, headers=self._headers
            )
            error = response_error(response)
            if error is not None:
                raise error

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
                attempts=number,
            )
            return message

        return self._run_request(attempt, cancel_requested=cancel_requested)

    def _run_request(
        self, attempt: Callable[[int], Message], *, cancel_requested: Callable[[], bool]
    ) -> Message:
        policy = HTTPRetryPolicy(
            self.provider_name,
            self.config.model,
            self.config.max_retries,
            self.config.retry_backoff_seconds,
            self.config.max_retry_backoff_seconds,
            read_timeout_fail_fast=True,
            timeout_as_request_error=self._timeout_as_request_error,
            reset_client=self._reset_transport_for_retry,
        )
        return run_with_retry(
            attempt,
            lambda error, number: policy.classify(
                error, number, cancel_requested=cancel_requested
            ),
            max_retries=self.config.max_retries,
            cancel_requested=cancel_requested,
            sleep=self._sleep,
        )

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

        with cancellation_watcher(cancel_requested, self._client.abort, repeat=True):
            try:
                return self._complete_impl(
                    messages,
                    tools,
                    cancel_requested=cancel_requested,
                )
            except ProviderError as exc:
                if cancel_requested():
                    raise ProviderCancelledError() from exc
                raise

    def close(self) -> None:
        """Close the owned HTTP client, if this provider created it."""
        if self._owns_client:
            self._client.close()

    def _reset_transport_for_retry(self) -> None:
        """Rebuild owned connection state after a transient network failure."""
        if self._owns_client:
            self._client.reset()

    def _chat_completions_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        path = self.config.chat_completions_path.lstrip("/")
        return f"{base_url}/{path}"
