"""openai_compat provider module."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

from yoke.agent.models import Message
from yoke.agent.models import MessagePhase
from yoke.agent.models import Role
from yoke.agent.models import ToolCall
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.base import ProviderRateLimitError
from yoke.ai.providers.base import ProviderServerError
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
from yoke.ai.providers.openai_compat.helpers import error_detail
from yoke.ai.providers.openai_compat.helpers import retry_after_seconds
from yoke.ai.providers.openai_compat.helpers import (
    should_retry_request_error,
)
from yoke.ai.providers.openai_compat.helpers import (
    thinking_levels_for_reasoning_effort,
)
from yoke.ai.providers.usage import parse_token_usage


class OpenAICompatibleConfig(BaseModel):
    """Configuration for providers exposing an OpenAI-compatible API."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    chat_completions_path: str = "/chat/completions"
    timeout_seconds: float | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    max_retries: int = 8
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 300.0
    reasoning_effort: str | None = None
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
                "reasoning_effort must be one of none, minimal, low, "
                "medium, high, xhigh, or max"
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


class OpenAICompatibleResponseMessage(BaseModel):
    """OpenAICompatibleResponseMessage."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    phase: MessagePhase | None = None
    reasoning_content: str | None = None

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_phase(cls, value: object) -> MessagePhase | None:
        """Normalize provider phase aliases when present."""
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"commentary", "preamble"}:
            return "commentary"
        if normalized in {"final_answer", "final"}:
            return "final_answer"
        return None

    def to_message(self) -> Message:
        """to_message."""
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            phase=self.phase,
            reasoning_content=self.reasoning_content,
        )


class OpenAICompatibleChoice(BaseModel):
    """OpenAICompatibleChoice."""

    message: OpenAICompatibleResponseMessage


class OpenAICompatibleChatCompletionResponse(BaseModel):
    """OpenAICompatibleChatCompletionResponse."""

    choices: list[OpenAICompatibleChoice]
    usage: dict[str, object] | None = None


class OpenAICompatibleProvider(Provider):
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
        self._owns_client = http_client is None
        self._sleep = sleep or time.sleep
        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            **config.headers,
        }
        self._client = http_client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            headers=self._headers,
            verify=False,  # noqa: S501
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

        last_error: ProviderError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.post(
                    self._chat_completions_url(),
                    json=payload,
                    headers=self._headers,
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderError("Provider request timed out.")
                if attempt < self.config.max_retries:
                    self._sleep(self._sleep_seconds(attempt))
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                last_error = self._handle_request_error(exc, attempt=attempt)
                if last_error is not None:
                    continue
                raise ProviderError(f"Provider request failed: {exc}") from exc

            last_error = self._handle_error_response(response, attempt=attempt)
            if last_error is not None:
                continue

            try:
                completion = OpenAICompatibleChatCompletionResponse.model_validate(
                    response.json()
                )
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
            return message

        if last_error is not None:
            raise last_error
        raise ProviderError("Provider request failed unexpectedly.")

    def close(self) -> None:
        """Close the owned HTTP client, if this provider created it."""
        if self._owns_client:
            self._client.close()

    def _backoff_seconds(self, attempt: int) -> float:
        return min(
            self.config.retry_backoff_seconds * (2**attempt),
            self.config.max_retry_backoff_seconds,
        )

    def _sleep_seconds(
        self, attempt: int, retry_after_seconds: float | None = None
    ) -> float:
        base_seconds = retry_after_seconds or self._backoff_seconds(attempt)
        jitter = secrets.randbelow(1000) / 1000 * min(1.0, base_seconds * 0.1)
        return min(base_seconds + jitter, self.config.max_retry_backoff_seconds)

    def _chat_completions_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        path = self.config.chat_completions_path.lstrip("/")
        return f"{base_url}/{path}"

    def _handle_request_error(
        self,
        error: httpx.RequestError,
        *,
        attempt: int,
    ) -> ProviderError | None:
        if not should_retry_request_error(error):
            return None
        provider_error = ProviderError(f"Provider request failed: {error}")
        if attempt < self.config.max_retries:
            self._sleep(self._sleep_seconds(attempt))
            return provider_error
        raise provider_error from error

    def _handle_error_response(
        self,
        response: httpx.Response,
        *,
        attempt: int,
    ) -> ProviderError | None:
        if response.status_code == 429:
            retry_after = retry_after_seconds(response)
            provider_error = ProviderRateLimitError(
                f"Provider request was rate limited: {error_detail(response)}",
                retry_after_seconds=retry_after,
            )
            if attempt < self.config.max_retries:
                self._sleep(self._sleep_seconds(attempt, retry_after))
                return provider_error
            raise provider_error
        if 500 <= response.status_code < 600:
            provider_error = ProviderServerError(
                f"Provider server error: {error_detail(response)}",
                status_code=response.status_code,
            )
            if attempt < self.config.max_retries:
                self._sleep(self._sleep_seconds(attempt))
                return provider_error
            raise provider_error
        if response.is_error:
            raise ProviderError(
                f"Provider request failed: {error_detail(response)}",
                status_code=response.status_code,
            )
        return None
