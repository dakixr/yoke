"""Context-aware agent capability registration."""

from __future__ import annotations

import os
import platform
import shutil
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from typing import TYPE_CHECKING

from yoke.agent.models import Message
from yoke.agent.tools.context import ModelIdentity
from yoke.agent.tools.context import ToolRegistrationContext
from yoke.agent.tools.context import ToolRegistrationResult
from yoke.agent.tools.context import never_cancel
from yoke.agent.tools.context import normalize_tool_registration
from yoke.agent.tools.context import resolve_model_identity
from yoke.ai.providers.base import Provider

if TYPE_CHECKING:
    from yoke.agent.tools.base import LocalTool


@dataclass(slots=True, frozen=True)
class CapabilityContext:
    """Context used to select and register agent capabilities."""

    root: Path
    home: Path
    provider: Provider
    model: ModelIdentity
    platform: str = platform.system().lower()
    os_name: str = os.name
    cancel_requested: Callable[[], bool] = never_cancel

    @classmethod
    def from_tool_registration(
        cls,
        context: ToolRegistrationContext,
    ) -> CapabilityContext:
        """Build a capability context from the legacy tool registration context."""
        return cls(
            root=context.root,
            home=context.home,
            provider=context.provider,
            model=context.model,
            cancel_requested=context.cancel_requested,
        )

    @classmethod
    def from_provider(
        cls,
        *,
        root: Path,
        home: Path,
        provider: Provider,
        cancel_requested: Callable[[], bool] = never_cancel,
    ) -> CapabilityContext:
        """Build a capability context by resolving model metadata from a provider."""
        return cls(
            root=root.resolve(),
            home=home.resolve(),
            provider=provider,
            model=resolve_model_identity(provider),
            cancel_requested=cancel_requested,
        )

    @property
    def provider_name(self) -> str:
        """Return the normalized provider name."""
        return self.model.provider_name

    @property
    def model_id(self) -> str | None:
        """Return the active model identifier."""
        return self.model.model_id

    @property
    def model_name(self) -> str | None:
        """Return the active model identifier."""
        return self.model.model_name

    @property
    def model_key(self) -> str | None:
        """Return the provider-qualified active model identifier."""
        return self.model.model_key

    @property
    def reasoning_effort(self) -> str | None:
        """Return active reasoning effort metadata when available."""
        return self.model.reasoning_effort

    def to_tool_registration(self) -> ToolRegistrationContext:
        """Convert to the legacy tool registration context."""
        return ToolRegistrationContext(
            root=self.root,
            home=self.home,
            provider=self.provider,
            model=self.model,
            cancel_requested=self.cancel_requested,
        )

    def executable(self, name: str) -> str | None:
        """Return the path to an executable on PATH, if present."""
        return shutil.which(name)


@dataclass(slots=True, frozen=True)
class CapabilityRegistration:
    """Tools and prompt messages contributed by one capability."""

    tools: tuple[LocalTool, ...] = ()
    system_messages: tuple[Message, ...] = ()

    @classmethod
    def from_tool_registration(
        cls,
        registration: ToolRegistrationResult,
    ) -> CapabilityRegistration:
        """Build a capability registration from a legacy tool registration."""
        normalized = normalize_tool_registration(registration)
        return cls(
            tools=tuple(normalized.tools),
            system_messages=tuple(
                message.model_copy(deep=True) for message in normalized.system_messages
            ),
        )


class BaseCapability(ABC):
    """Context-aware provider of one or more executable tools."""

    name: ClassVar[str]
    description: ClassVar[str] = ""

    def is_available(self, context: CapabilityContext) -> bool:
        """Return whether this capability should register in the context."""
        del context
        return True

    @abstractmethod
    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        """Register tools and system messages for the context."""
        raise NotImplementedError


@dataclass(slots=True, frozen=True)
class CapabilityResolution:
    """Resolved capability registrations flattened for provider use."""

    registrations: tuple[tuple[BaseCapability, CapabilityRegistration], ...]

    @property
    def tools(self) -> tuple[LocalTool, ...]:
        """Return all resolved tools in capability order."""
        return tuple(
            tool
            for _capability, registration in self.registrations
            for tool in registration.tools
        )

    @property
    def system_messages(self) -> tuple[Message, ...]:
        """Return all resolved capability system messages in order."""
        return tuple(
            message.model_copy(deep=True)
            for _capability, registration in self.registrations
            for message in registration.system_messages
        )


class CapabilityResolver:
    """Resolve capabilities for an agent context."""

    def __init__(self, capabilities: Sequence[BaseCapability]) -> None:
        self._capabilities = tuple(capabilities)

    @property
    def capabilities(self) -> tuple[BaseCapability, ...]:
        """Return the configured capabilities."""
        return self._capabilities

    def resolve(self, context: CapabilityContext) -> CapabilityResolution:
        """Resolve all available capabilities for the context."""
        registrations: list[tuple[BaseCapability, CapabilityRegistration]] = []
        for capability in self._capabilities:
            if not capability.is_available(context):
                continue
            registration = capability.register(context)
            registrations.append((capability, registration))
        return CapabilityResolution(registrations=tuple(registrations))


type CapabilityInput = BaseCapability | type[BaseCapability]


def instantiate_capabilities(
    capabilities: Iterable[CapabilityInput],
) -> tuple[BaseCapability, ...]:
    """Instantiate capability classes while preserving capability instances."""
    resolved: list[BaseCapability] = []
    for capability in capabilities:
        if isinstance(capability, BaseCapability):
            resolved.append(capability)
            continue
        if isinstance(capability, type) and issubclass(capability, BaseCapability):
            resolved.append(capability())
            continue
        raise TypeError(
            "Capabilities must be BaseCapability instances or classes. "
            f"Got {capability!r}."
        )
    return tuple(resolved)
