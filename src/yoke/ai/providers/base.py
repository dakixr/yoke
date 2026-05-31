"""Base provider protocol and error types for AI providers."""

from __future__ import annotations

from typing import Protocol
from typing import runtime_checkable

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message

DEFAULT_THINKING_LEVELS = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
)


class ProviderError(RuntimeError):
    """Base error raised by AI provider implementations."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        partial_messages: list[Message] | None = None,
        partial_conversation_entries: list[ConversationEntry] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.partial_messages = partial_messages
        self.partial_conversation_entries = partial_conversation_entries


class ProviderRateLimitError(ProviderError):
    """Error raised when the provider returns a rate limit response."""

    def __init__(
        self, message: str, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_seconds = retry_after_seconds


class ProviderServerError(ProviderError):
    """Error raised for 5xx server errors from the provider."""

    pass


class ProviderModelInfo(BaseModel):
    """Provider-advertised metadata for one selectable model."""

    id: str
    display_name: str
    context_window_tokens: int
    thinking_levels: tuple[str, ...] = Field(default_factory=tuple)
    default_thinking_level: str | None = None
    supports_image_inputs: bool | None = None

    @field_validator("id", "display_name")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """Ensure provider model metadata uses non-empty identifiers."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider model metadata must not be empty.")
        return normalized

    @field_validator("context_window_tokens")
    @classmethod
    def validate_context_window_tokens(cls, value: int) -> int:
        """Ensure context window metadata is a positive token count."""
        if value <= 0:
            raise ValueError("context_window_tokens must be positive")
        return value

    @field_validator("thinking_levels")
    @classmethod
    def validate_thinking_levels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize and validate thinking levels for a provider model."""
        normalized = tuple(level.strip().lower() for level in value if level.strip())
        if not normalized:
            raise ValueError("thinking_levels must not be empty")
        return normalized

    @field_validator("default_thinking_level")
    @classmethod
    def validate_default_thinking_level(cls, value: str | None) -> str | None:
        """Normalize the optional default thinking level."""
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("default_thinking_level must not be empty")
        return normalized


@runtime_checkable
class ModelCatalogProvider(Protocol):
    """Protocol for providers that expose selectable model metadata."""

    provider_name: str

    def list_models(self) -> list[ProviderModelInfo]:
        """Return all selectable models for this provider."""
        ...

    def current_model_id(self) -> str | None:
        """Return the currently selected model identifier."""
        ...

    def current_model_info(self) -> ProviderModelInfo | None:
        """Return metadata for the currently selected model."""
        ...

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        """Switch the active model, optionally updating thinking level."""
        ...


class Provider(Protocol):
    """Protocol for AI provider implementations."""

    supports_image_inputs: bool
    max_images_per_message: int | None

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Send messages to the provider and return the assistant response."""
        ...
