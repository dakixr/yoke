"""Public provider-aware contexts for tool registration and execution."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.models import Message
from yoke.ai.providers.base import ModelCatalogProvider
from yoke.ai.providers.base import Provider

if TYPE_CHECKING:
    from yoke.agent.tools.base import LocalTool


def never_cancel() -> bool:
    """Return False for runtimes without an external cancellation source."""
    return False


@dataclass(slots=True, frozen=True)
class ModelIdentity:
    """Stable provider and model identity exposed to tools."""

    provider_name: str
    model_id: str | None = None
    reasoning_effort: str | None = None

    @property
    def model_name(self) -> str | None:
        """Return the selected provider model identifier."""
        return self.model_id

    @property
    def model_key(self) -> str | None:
        """Return the provider-qualified model identifier."""
        if self.model_id is None:
            return None
        return f"{self.provider_name}:{self.model_id}"


@dataclass(slots=True, frozen=True)
class ToolRegistrationContext:
    """Context passed to a provider-aware tool registration callback."""

    root: Path
    home: Path
    provider: Provider
    model: ModelIdentity
    cancel_requested: Callable[[], bool] = never_cancel

    @property
    def provider_name(self) -> str:
        """Return the stable provider identifier."""
        return self.model.provider_name

    @property
    def model_id(self) -> str | None:
        """Return the selected model identifier."""
        return self.model.model_id

    @property
    def model_name(self) -> str | None:
        """Return the selected model identifier."""
        return self.model.model_name

    @property
    def model_key(self) -> str | None:
        """Return the provider-qualified model identifier."""
        return self.model.model_key

    @property
    def reasoning_effort(self) -> str | None:
        """Return the selected reasoning effort."""
        return self.model.reasoning_effort


@dataclass(slots=True, frozen=True)
class ToolRuntimeContext:
    """Current provider and workspace context available during tool execution."""

    root: Path
    home: Path
    provider: Provider
    model: ModelIdentity
    cancel_requested: Callable[[], bool] = never_cancel

    @property
    def provider_name(self) -> str:
        """Return the stable provider identifier."""
        return self.model.provider_name

    @property
    def model_id(self) -> str | None:
        """Return the selected model identifier."""
        return self.model.model_id

    @property
    def model_name(self) -> str | None:
        """Return the selected model identifier."""
        return self.model.model_name

    @property
    def model_key(self) -> str | None:
        """Return the provider-qualified model identifier."""
        return self.model.model_key

    @property
    def reasoning_effort(self) -> str | None:
        """Return the selected reasoning effort."""
        return self.model.reasoning_effort


@dataclass(slots=True, frozen=True)
class ToolRegistrationResult:
    """Tools and system instructions contributed by one registration."""

    tools: Iterable["LocalTool"]
    system_messages: Iterable[Message] = ()


type ToolRegistration = Iterable["LocalTool"] | ToolRegistrationResult
type RegisterTools = Callable[
    [ToolRegistrationContext],
    ToolRegistration,
]


def normalize_tool_registration(value: ToolRegistration) -> ToolRegistrationResult:
    """Normalize legacy iterable returns into a structured registration result."""
    if isinstance(value, ToolRegistrationResult):
        system_messages: list[Message] = []
        for message in value.system_messages:
            if not isinstance(message, Message):
                raise TypeError(
                    "Tool registration system_messages must contain Message values"
                )
            if message.role != "system":
                raise ValueError(
                    "Tool registration system_messages must have role='system'"
                )
            system_messages.append(message.model_copy(deep=True))
        return ToolRegistrationResult(
            tools=tuple(value.tools),
            system_messages=tuple(system_messages),
        )
    return ToolRegistrationResult(tools=tuple(value))


def resolve_model_identity(provider: Provider) -> ModelIdentity:
    """Resolve normalized public identity metadata from a provider."""
    provider_name = _provider_name(provider)
    config = getattr(provider, "config", None)
    model_id = _model_id(provider, config)
    reasoning_effort = getattr(config, "reasoning_effort", None)
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None
    else:
        reasoning_effort = reasoning_effort.strip().lower()
    return ModelIdentity(
        provider_name=provider_name,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
    )


def _provider_name(provider: object) -> str:
    candidate = getattr(provider, "provider_name", None)
    if not isinstance(candidate, str) or not candidate.strip():
        config = getattr(provider, "config", None)
        candidate = getattr(config, "provider_name", None)
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = getattr(config, "name", None)
    if not isinstance(candidate, str) or not candidate.strip():
        candidate = provider.__class__.__name__
    return candidate.strip().lower()


def _model_id(provider: object, config: object) -> str | None:
    if isinstance(provider, ModelCatalogProvider):
        model_id = provider.current_model_id()
    else:
        model_id = getattr(config, "model", None)
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    return model_id.strip()
