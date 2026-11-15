"""Base provider protocol and error types for AI providers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self, cast, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    import httpx

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


class ProviderModelConfig(Protocol):
    """Public model fields exposed by every built-in provider configuration."""

    @property
    def model(self) -> str: ...

    @property
    def reasoning_effort(self) -> str | None: ...


class Closeable(Protocol):
    """The public client lifecycle needed by the built-in provider defaults."""

    def close(self) -> None: ...


class BaseProvider[ConfigT: BaseModel, ClientT: Closeable]:
    """Defaults for built-in providers; plugins can still implement Provider.

    Each subclass owns its config and transport. The two transport hooks cover
    construction and borrowing without introspecting private client state.
    """

    config: ConfigT
    provider_name: str
    _client: ClientT
    _owns_client: bool
    _sleep: Callable[[float], None]
    supports_image_inputs: bool | None = None
    max_images_per_message: int | None = None
    supports_image_generation: bool = False

    def _new_client(self) -> ClientT:
        raise NotImplementedError

    def _fork_client(self) -> httpx.Client | None:
        raise NotImplementedError

    def fork_for_turn(self) -> Self:
        """Copy request state and preserve borrowed transport ownership."""
        factory = cast(Callable[..., Self], type(self))
        return factory(
            self.config.model_copy(deep=True),
            http_client=self._fork_client(),
            sleep=self._sleep,
        )

    def list_models(self) -> list[ProviderModelInfo]:
        raise NotImplementedError

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        from yoke.ai.providers.model_selection import set_config_model_from_catalog

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
        return self.complete_with_cancel(messages, tools, cancel_requested=never_cancel)

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        return self._with_request_cancellation(
            lambda: self._complete_impl(
                messages, tools, cancel_requested=cancel_requested
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
        raise NotImplementedError

    def _with_request_cancellation(
        self, action: Callable[[], Message], *, cancel_requested: Callable[[], bool]
    ) -> Message:
        from yoke.ai.providers.cancellation import cancellation_watcher

        if not self._owns_client:
            return action()

        def rebuild() -> None:
            self._client = self._new_client()

        with cancellation_watcher(
            cancel_requested, lambda: self._client.close(), rebuild=rebuild
        ):
            return action()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Normalized provider metadata for attribution and model selection."""

    name: str
    model: str | None
    reasoning_effort: str | None


def provider_identity(provider: object) -> ProviderIdentity:
    """Read provider metadata without guessing a name from its Python type."""

    def text(value: object) -> str | None:
        return value.strip() or None if isinstance(value, str) else None

    if isinstance(provider, BaseProvider):
        builtin = cast(BaseProvider[BaseModel, Closeable], provider)
        config = cast(ProviderModelConfig, builtin.config)
        model = (
            provider.current_model_id()
            if isinstance(provider, ModelCatalogProvider)
            else config.model
        )
        return ProviderIdentity(
            name=text(builtin.provider_name) or "unknown",
            model=text(model),
            reasoning_effort=text(config.reasoning_effort),
        )
    config = getattr(provider, "config", None)
    current_model = getattr(provider, "current_model_id", None)
    model = (
        current_model() if callable(current_model) else getattr(config, "model", None)
    )
    return ProviderIdentity(
        name=text(getattr(provider, "provider_name", None)) or "unknown",
        model=text(model),
        reasoning_effort=text(getattr(config, "reasoning_effort", None)),
    )


def close_provider(provider: object) -> None:
    """Close built-ins directly and preserve optional plugin lifecycle hooks."""
    if isinstance(provider, BaseProvider):
        cast(BaseProvider[BaseModel, Closeable], provider).close()
        return
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def fork_provider(provider: Provider) -> Provider:
    """Fork explicitly, warning when a custom provider offers no isolation.

    Built-ins preserve injected transports without taking ownership of them.
    Custom providers must implement fork_for_turn for independent request state.
    For compatibility, missing hooks share the instance with a warning rather
    than guessing a constructor and silently dropping injected dependencies.
    """
    custom_fork = getattr(provider, "fork_for_turn", None)
    if callable(custom_fork):
        forked = cast(Provider, custom_fork())
        if forked is not provider:
            return forked
        reason = "fork_for_turn() returned the original instance"
    else:
        reason = "does not implement fork_for_turn()"
    logging.getLogger(__name__).warning(
        "Provider %s %s; request state is shared. "
        "Implement fork_for_turn() to return an independent provider.",
        type(provider).__name__,
        reason,
    )
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
) -> Message:
    """Use provider-native cancellation when available."""
    if cancel_requested is not None and cancel_requested():
        raise ProviderCancelledError()
    if isinstance(provider, CancellableProvider):
        try:
            response = provider.complete_with_cancel(
                messages,
                tools,
                cancel_requested=cancel_requested or never_cancel,
            )
        except ProviderError as exc:
            if cancel_requested is not None and cancel_requested():
                raise ProviderCancelledError() from exc
            raise
    else:
        response = provider.complete(messages, tools)
    # usage_log imports provider_identity; importing here avoids a module cycle.
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
