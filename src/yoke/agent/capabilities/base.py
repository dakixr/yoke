"""Base contracts for provider-aware tool capabilities."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from yoke.agent.models import Message
from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.context import ToolRegistrationContext
from yoke.agent.tools.context import ToolRuntimeContext


@dataclass(slots=True, frozen=True)
class CapabilityRegistration:
    """Concrete tools and instructions for one capability."""

    capability_id: str
    tools: tuple[LocalTool, ...]
    system_messages: tuple[Message, ...] = ()


class BaseCapability(ABC):
    """A provider/model-aware wrapper around one or more concrete tools."""

    capability_id: str

    def resolve(
        self,
        context: ToolRegistrationContext,
    ) -> CapabilityRegistration:
        """Resolve this capability into validated concrete tools."""
        capability_id = self._validated_capability_id()
        tools = tuple(self.build_tools(context))
        for tool in tools:
            if not isinstance(tool, LocalTool):
                raise TypeError(
                    f"Capability {capability_id!r} returned a non-tool value."
                )
        messages = tuple(self.system_messages(context))
        for message in messages:
            if not isinstance(message, Message):
                raise TypeError(f"Capability {capability_id!r} returned a non-message.")
            if message.role != "system":
                raise ValueError(
                    f"Capability {capability_id!r} messages must be system."
                )
        self._bind_runtime_context(context, tools)
        return CapabilityRegistration(
            capability_id=capability_id,
            tools=tools,
            system_messages=messages,
        )

    @abstractmethod
    def build_tools(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[LocalTool]:
        """Return the concrete tools that implement this capability."""
        raise NotImplementedError

    def system_messages(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[Message]:
        """Return optional system messages contributed by this capability."""
        del context
        return ()

    def _validated_capability_id(self) -> str:
        capability_id = getattr(self, "capability_id", None)
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ValueError("Capability classes must define capability_id.")
        return capability_id.strip()

    def _bind_runtime_context(
        self,
        context: ToolRegistrationContext,
        tools: tuple[LocalTool, ...],
    ) -> None:
        runtime_context = ToolRuntimeContext(
            root=context.root,
            home=context.home,
            provider=context.provider,
            model=context.model,
            cancel_requested=context.cancel_requested,
        )
        for tool in tools:
            tool.bind_runtime_context(runtime_context)
