"""Base provider protocol and error types for AI providers."""

from __future__ import annotations

import time
from collections.abc import Callable
from collections.abc import Iterable
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


class ProviderCancelledError(ProviderError):
    """Error raised when a provider request is cancelled by the caller."""

    def __init__(self, message: str = "Provider request cancelled.") -> None:
        super().__init__(message)


class ProviderModelInfo(BaseModel):
    """Provider-advertised metadata for one selectable model."""

    id: str
    display_name: str
    context_window_tokens: int
    thinking_levels: tuple[str, ...] = Field(default_factory=tuple)
    default_thinking_level: str | None = None
    supports_image_inputs: bool | None = None
    system_messages: tuple[Message, ...] = Field(default_factory=tuple)

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
        return tuple(level.strip().lower() for level in value if level.strip())

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

    @field_validator("system_messages")
    @classmethod
    def validate_system_messages(
        cls, value: tuple[Message, ...]
    ) -> tuple[Message, ...]:
        """Ensure provider/model prompt contributions are system messages."""
        messages: list[Message] = []
        for message in value:
            if message.role != "system":
                raise ValueError("system_messages must have role='system'")
            messages.append(message.model_copy(deep=True))
        return tuple(messages)


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

    supports_image_inputs: bool = False
    max_images_per_message: int | None = None

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Send messages to the provider and return the assistant response."""
        ...


@runtime_checkable
class CancellableProvider(Protocol):
    """Optional protocol for providers that can abort in-flight requests."""

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        """Send messages and abort promptly when cancellation is requested."""
        ...


def complete_with_cancel(
    provider: Provider,
    messages: list[Message],
    tools: list[dict[str, object]],
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> Message:
    """Run a provider completion, using provider-native cancellation when available."""
    if cancel_requested is not None and cancel_requested():
        raise ProviderCancelledError()
    if isinstance(provider, CancellableProvider):
        return provider.complete_with_cancel(
            messages,
            tools,
            cancel_requested=cancel_requested or never_cancel,
        )
    response = provider.complete(messages, tools)
    if cancel_requested is not None and cancel_requested():
        raise ProviderCancelledError()
    return response


def start_provider_turn(provider: Provider) -> None:
    """Notify a provider that a new logical user turn is starting."""
    start_turn = getattr(provider, "start_turn", None)
    if callable(start_turn):
        start_turn()


def never_cancel() -> bool:
    """Return false for APIs that require a cancellation callback."""
    return False


def sleep_with_cancel(
    seconds: float,
    *,
    cancel_requested: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: float = 0.1,
) -> None:
    """Sleep in small increments so provider retry backoff can be cancelled."""
    if sleep is not time.sleep:
        if cancel_requested():
            raise ProviderCancelledError()
        sleep(max(0.0, seconds))
        if cancel_requested():
            raise ProviderCancelledError()
        return
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_requested():
            raise ProviderCancelledError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval_seconds, remaining))


@runtime_checkable
class ProviderSystemMessageProvider(Protocol):
    """Optional provider hook for active model system instructions."""

    def current_model_system_messages(self) -> Iterable[Message]:
        """Return system messages for the provider's active model."""
        ...


def provider_system_messages(provider: Provider) -> list[Message]:
    """Return validated system messages for a provider's active model."""
    messages: list[Message] = []
    if isinstance(provider, ModelCatalogProvider):
        model_info = provider.current_model_info()
        if model_info is not None:
            messages.extend(model_info.system_messages)
    if isinstance(provider, ProviderSystemMessageProvider):
        messages.extend(provider.current_model_system_messages())
    return _copy_system_messages(messages)


def insert_provider_system_messages(
    messages: list[Message],
    provider: Provider,
) -> list[Message]:
    """Insert provider/model system messages after leading system messages."""
    provider_messages = provider_system_messages(provider)
    if not provider_messages:
        return [message.model_copy(deep=True) for message in messages]
    resolved = [message.model_copy(deep=True) for message in messages]
    insert_at = 0
    while insert_at < len(resolved) and resolved[insert_at].role == "system":
        insert_at += 1
    return [
        *resolved[:insert_at],
        *provider_messages,
        *resolved[insert_at:],
    ]


def _copy_system_messages(messages: Iterable[Message]) -> list[Message]:
    copied: list[Message] = []
    for message in messages:
        if not isinstance(message, Message):
            raise TypeError("Provider system messages must contain Message values")
        if message.role != "system":
            raise ValueError("Provider system messages must have role='system'")
        copied.append(message.model_copy(deep=True))
    return copied
