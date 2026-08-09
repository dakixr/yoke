"""Base provider protocol and error types for AI providers."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING
from typing import Literal
from typing import Protocol
from typing import cast
from typing import runtime_checkable

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

if TYPE_CHECKING:
    from yoke.agent.models import ConversationEntry

from yoke.agent.models import Message

ProviderEventHandler = Callable[[str, dict[str, object]], None]
_PROVIDER_EVENT_HANDLER: ContextVar[ProviderEventHandler | None] = ContextVar(
    "yoke_provider_event_handler",
    default=None,
)


@contextmanager
def provider_event_handler(
    handler: ProviderEventHandler | None,
) -> Iterator[None]:
    """Route provider telemetry from the current request to a handler."""
    token = _PROVIDER_EVENT_HANDLER.set(handler)
    try:
        yield
    finally:
        _PROVIDER_EVENT_HANDLER.reset(token)


def emit_provider_event(event: str, payload: dict[str, object]) -> None:
    """Emit provider telemetry when the caller installed a handler."""
    handler = _PROVIDER_EVENT_HANDLER.get()
    if handler is not None:
        handler(event, payload)


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

    @model_validator(mode="after")
    def validate_default_is_supported(self) -> ProviderModelInfo:
        """Require a declared default to be one of the supported levels."""
        if (
            self.default_thinking_level is not None
            and self.default_thinking_level not in self.thinking_levels
        ):
            raise ValueError("default_thinking_level must appear in thinking_levels")
        return self

    @field_validator("system_messages")
    @classmethod
    def validate_system_messages(
        cls, value: tuple[Message, ...]
    ) -> tuple[Message, ...]:
        """Require provider/model prompt contributions to be system messages."""
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

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Send messages to the provider and return the assistant response."""
        ...


ResponseContinuity = Literal["continue", "reset", "isolated"]


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    """Stable request metadata supplied by the agent runtime."""

    cache_scope: str
    response_continuity: ResponseContinuity = "continue"


@runtime_checkable
class ContextualProvider(Protocol):
    """Optional protocol for providers using runtime request metadata."""

    def complete_with_context(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        request_context: ProviderRequestContext,
    ) -> Message:
        """Complete a request with stable conversation metadata."""
        ...


def fork_provider(provider: Provider) -> Provider:
    """Create a request-isolated provider when its constructor supports it."""
    custom_fork = getattr(provider, "fork_for_turn", None)
    if callable(custom_fork):
        return cast(Provider, custom_fork())
    config = getattr(provider, "config", None)
    if config is None:
        return provider
    copy_config = getattr(config, "model_copy", None)
    cloned_config = (
        copy_config(deep=True) if callable(copy_config) else deepcopy(config)
    )
    constructor = cast(Callable[..., Provider], type(provider))
    sleep = getattr(provider, "_sleep", None)
    try:
        return constructor(cloned_config, **({"sleep": sleep} if sleep else {}))
    except (AttributeError, TypeError, ValueError):
        try:
            return constructor(cloned_config)
        except (AttributeError, TypeError, ValueError):
            return provider


@runtime_checkable
class CancellableProvider(Protocol):
    """Optional protocol for providers that support cooperative cancellation."""

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        """Complete while observing the supplied cancellation callback."""
        ...


def complete_with_cancel(
    provider: Provider,
    messages: list[Message],
    tools: list[dict[str, object]],
    *,
    cancel_requested: Callable[[], bool] | None = None,
    request_context: ProviderRequestContext | None = None,
) -> Message:
    """Use provider-native cancellation when available."""
    if cancel_requested is not None and cancel_requested():
        raise ProviderCancelledError()
    if request_context is not None and isinstance(provider, ContextualProvider):
        response = provider.complete_with_context(
            messages,
            tools,
            request_context=request_context,
        )
    elif isinstance(provider, CancellableProvider):
        try:
            response = provider.complete_with_cancel(
                messages,
                tools,
                cancel_requested=cancel_requested or _never_cancel,
            )
        except ProviderError as exc:
            if cancel_requested is not None and cancel_requested():
                raise ProviderCancelledError() from exc
            raise
    else:
        response = provider.complete(messages, tools)
    from yoke.ai.providers.usage_log import record_provider_usage

    record_provider_usage(provider, response)
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


_never_cancel = never_cancel


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
