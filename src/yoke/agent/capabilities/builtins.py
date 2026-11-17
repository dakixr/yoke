"""Built-in provider-aware tool capabilities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from yoke.agent.capabilities.base import BaseCapability, CapabilityRegistration
from yoke.agent.models import Message
from yoke.agent.tools import (
    ApplyPatchTool,
    AttachImageTool,
    EditTool,
    ExecCommandTool,
    ExtractFileContextTool,
    FdTool,
    FindTool,
    GrepTool,
    ImageGenerationTool,
    LocalTool,
    LsTool,
    ProcessCancelTool,
    ProcessInputTool,
    ProcessReadTool,
    PythonExecTool,
    ReadTool,
    RipgrepTool,
    ToolRegistrationContext,
    WebFetchTool,
    WebResearchTool,
    WebSearchTool,
    WorkspaceTool,
    WriteTool,
)
from yoke.agent.tools.apply_patch.instructions import (
    APPLY_PATCH_INSTRUCTIONS,
)
from yoke.agent.tools.image_generation import provider_supports_image_generation
from yoke.agent.tools.mcp import LazyMcpManager, register_mcp_tools
from yoke.agent.tools.search_common import find_search_binary


@dataclass(slots=True, frozen=True)
class ToolClassCapability(BaseCapability):
    """Capability implemented by static concrete tool classes."""

    capability_id: str
    tool_classes: tuple[type[LocalTool], ...]

    def build_tools(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[LocalTool]:
        """Bind every configured concrete tool class."""
        return tuple(
            bind_tool_class(tool_class, context) for tool_class in self.tool_classes
        )


class FileWriteCapability(BaseCapability):
    """Model-aware file-writing capability."""

    capability_id = "file.write"

    def build_tools(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[LocalTool]:
        """Use patching for GPT-ish models, edit/write otherwise."""
        if model_prefers_apply_patch(context.model_id):
            return (bind_tool_class(ApplyPatchTool, context),)
        return (
            bind_tool_class(EditTool, context),
            bind_tool_class(WriteTool, context),
        )

    def system_messages(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[Message]:
        """Describe the selected patch protocol only when it is available."""
        if model_prefers_apply_patch(context.model_id):
            return (Message.system(APPLY_PATCH_INSTRUCTIONS),)
        return ()


class ImageAttachCapability(ToolClassCapability):
    """Attach-image capability hidden from image-less providers."""

    def resolve(
        self,
        context: ToolRegistrationContext,
    ) -> CapabilityRegistration:
        """Return no tools when the active provider rejects images."""
        if context_supports_image_inputs(context) is False:
            return CapabilityRegistration(self.capability_id, ())
        return super().resolve(context)


class ImageGenerationCapability(ToolClassCapability):
    """Generate images only when the active provider exposes the API."""

    def resolve(
        self,
        context: ToolRegistrationContext,
    ) -> CapabilityRegistration:
        if not provider_supports_image_generation(context.provider):
            return CapabilityRegistration(self.capability_id, ())
        return super().resolve(context)


class FileSearchCapability(BaseCapability):
    """Select native search executables with portable fallbacks."""

    capability_id = "file.search"

    def build_tools(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[LocalTool]:
        native: list[LocalTool] = []
        if find_search_binary("rg") is not None:
            native.append(bind_tool_class(RipgrepTool, context))
        if find_search_binary("fd") is not None:
            native.append(bind_tool_class(FdTool, context))
        if native:
            return tuple(native)
        return tuple(
            bind_tool_class(tool_class, context)
            for tool_class in (GrepTool, FindTool, LsTool)
        )


class McpCapability(BaseCapability):
    """Compact MCP facade capability."""

    capability_id = "mcp"

    def build_tools(
        self,
        context: ToolRegistrationContext,
    ) -> Iterable[LocalTool]:
        """Expose only the low-context MCP inspect/call tools."""
        policy = context.session.mcp_policy
        manager = LazyMcpManager(
            root=context.root,
            home=context.home,
            session_policy=policy if policy is not None else None,
            base_environment=context.session.environment,
        )
        return register_mcp_tools(manager)


def builtin_capabilities() -> tuple[BaseCapability, ...]:
    """Return built-in capabilities in stable display order."""
    return (
        ToolClassCapability("file.read", (ReadTool, ExtractFileContextTool)),
        FileWriteCapability(),
        ToolClassCapability(
            "shell",
            (
                ExecCommandTool,
                PythonExecTool,
                ProcessInputTool,
                ProcessReadTool,
                ProcessCancelTool,
            ),
        ),
        ImageAttachCapability("image.attach", (AttachImageTool,)),
        ImageGenerationCapability("image.generate", (ImageGenerationTool,)),
        ToolClassCapability("web.fetch", (WebFetchTool,)),
        ToolClassCapability("web.search", (WebSearchTool,)),
        ToolClassCapability("web.research", (WebResearchTool,)),
        FileSearchCapability(),
        McpCapability(),
    )


def create_builtin_capabilities(
    context: ToolRegistrationContext,
) -> list[CapabilityRegistration]:
    """Resolve all built-in capabilities for the active context."""
    return [capability.resolve(context) for capability in builtin_capabilities()]


def resolve_builtin_capability(
    capability_id: str,
    context: ToolRegistrationContext,
) -> CapabilityRegistration:
    """Resolve one built-in capability without constructing the others."""
    for capability in builtin_capabilities():
        if capability.capability_id == capability_id:
            return capability.resolve(context)
    raise ValueError(f"Unknown built-in tool capability: {capability_id}")


def create_builtin_tool_entries(
    context: ToolRegistrationContext,
) -> list[tuple[LocalTool, str]]:
    """Create built-in tools with capability metadata."""
    return [
        (tool, capability.capability_id)
        for capability in create_builtin_capabilities(context)
        for tool in capability.tools
    ]


def known_builtin_capability_ids() -> set[str]:
    """Return stable built-in capability IDs."""
    return {capability.capability_id for capability in builtin_capabilities()}


def bind_tool_class(
    tool_class: type[LocalTool],
    context: ToolRegistrationContext,
) -> LocalTool:
    """Bind one concrete tool class for a capability context."""
    kwargs: dict[str, object] = {"cancel_requested": context.cancel_requested}
    if issubclass(tool_class, WorkspaceTool):
        kwargs["root"] = context.root
    return tool_class.bind(**kwargs)


def model_prefers_apply_patch(model_id: str | None) -> bool:
    """Return whether the model should receive the patch interface."""
    return isinstance(model_id, str) and "gpt" in model_id.lower()


def context_supports_image_inputs(
    context: ToolRegistrationContext,
) -> bool | None:
    """Return image-input support for the current provider/model, if known."""
    if getattr(context.provider, "provider_name", None) == "unavailable":
        return None
    if isinstance(context.model.supports_image_inputs, bool):
        return context.model.supports_image_inputs
    current_model_info = getattr(context.provider, "current_model_info", None)
    if callable(current_model_info):
        model_info = current_model_info()
        model_support = getattr(model_info, "supports_image_inputs", None)
        if isinstance(model_support, bool):
            return model_support
    provider_support = getattr(context.provider, "supports_image_inputs", None)
    return provider_support if isinstance(provider_support, bool) else None
