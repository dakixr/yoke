"""Model-aware selection for the built-in file writing capability."""

from __future__ import annotations

from yoke.agent.tools.context import ToolRegistrationContext
from yoke.agent.tools.context import ToolRegistrationResult


def register_write_tool(context: ToolRegistrationContext) -> ToolRegistrationResult:
    """Register the preferred writing tool for the selected model."""
    from yoke.agent.capabilities.builtin import FileEditCapability
    from yoke.agent.capabilities.core import CapabilityContext

    registration = FileEditCapability().register(
        CapabilityContext.from_tool_registration(context)
    )
    return ToolRegistrationResult(
        tools=registration.tools,
        system_messages=registration.system_messages,
    )


def model_prefers_apply_patch(model_id: str | None) -> bool:
    """Return whether the model should receive the apply-patch interface."""
    return isinstance(model_id, str) and "gpt" in model_id.lower()
