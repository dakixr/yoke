"""Built-in yoke agent capabilities."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.capabilities.core import BaseCapability
from yoke.agent.capabilities.core import CapabilityContext
from yoke.agent.capabilities.core import CapabilityRegistration
from yoke.agent.models import Message
from yoke.agent.multimodal import provider_supports_image_inputs
from yoke.agent.tools.apply_patch import ApplyPatchTool
from yoke.agent.tools.attach_image import AttachImageTool
from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.command import CommandTool
from yoke.agent.tools.command import WriteStdinTool
from yoke.agent.tools.document_extract import ExtractFileContextTool
from yoke.agent.tools.edit import EditTool
from yoke.agent.tools.image_generation import ImageGenerationTool
from yoke.agent.tools.image_generation import provider_supports_image_generation
from yoke.agent.tools.mcp import register_mcp_tools
from yoke.agent.tools.python.execute import PythonExecTool
from yoke.agent.tools.read import ReadTool
from yoke.agent.tools.rg import RipgrepTool
from yoke.agent.tools.search import FindTool
from yoke.agent.tools.search import GrepTool
from yoke.agent.tools.search import LsTool
from yoke.agent.tools.web import WebFetchTool
from yoke.agent.tools.web import WebResearchTool
from yoke.agent.tools.web import WebSearchTool
from yoke.agent.tools.write_file import WriteTool

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
APPLY_PATCH_SYSTEM_PROMPT = (
    (_TOOLS_DIR / "apply_patch" / "prompt.md").read_text(encoding="utf-8").strip()
)
EDIT_SYSTEM_PROMPT = (_TOOLS_DIR / "edit_prompt.md").read_text(encoding="utf-8").strip()


def bind_tool(
    tool: type[LocalTool],
    context: CapabilityContext,
    **extra: object,
) -> LocalTool:
    """Bind a tool with common capability context."""
    bind_context: dict[str, object] = {
        "provider": context.provider,
        "cancel_requested": context.cancel_requested,
        **extra,
    }
    return tool.bind(**bind_context)


def bind_workspace_tool(
    tool: type[LocalTool],
    context: CapabilityContext,
    **extra: object,
) -> LocalTool:
    """Bind a workspace tool with common capability context."""
    return bind_tool(
        tool,
        context,
        root=context.root,
        home=context.home,
        **extra,
    )


class FileReadCapability(BaseCapability):
    """Read UTF-8 text from workspace files."""

    name = "file.read"
    description = "Read workspace files."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(tools=(bind_workspace_tool(ReadTool, context),))


class FileContextCapability(BaseCapability):
    """Extract readable context from documents and common binary files."""

    name = "file.context"
    description = "Extract readable context from workspace files."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(
            tools=(bind_workspace_tool(ExtractFileContextTool, context),)
        )


class FileSearchCapability(BaseCapability):
    """Search workspace files using ripgrep when available."""

    name = "file.search"
    description = "Search and list workspace files."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        if context.executable("rg") is not None:
            return CapabilityRegistration(
                tools=(bind_workspace_tool(RipgrepTool, context),)
            )
        return CapabilityRegistration(
            tools=(
                bind_workspace_tool(GrepTool, context),
                bind_workspace_tool(FindTool, context),
                bind_workspace_tool(LsTool, context),
            )
        )


class FileEditCapability(BaseCapability):
    """Edit workspace files with the preferred model-specific interface."""

    name = "file.edit"
    description = "Edit and write files using the active model's preferred interface."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        prefers_patch = model_prefers_apply_patch(context.model_id)
        if prefers_patch:
            tools = (bind_workspace_tool(ApplyPatchTool, context),)
            system_prompt = APPLY_PATCH_SYSTEM_PROMPT
        else:
            tools = (
                bind_workspace_tool(EditTool, context),
                bind_workspace_tool(WriteTool, context),
            )
            system_prompt = EDIT_SYSTEM_PROMPT
        return CapabilityRegistration(
            tools=tools,
            system_messages=(Message.system(system_prompt),),
        )


class CommandExecutionCapability(BaseCapability):
    """Execute shell commands and Python code in the workspace."""

    name = "command_execution"
    description = "Run shell commands and Python code in the workspace."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(
            tools=(
                bind_workspace_tool(CommandTool, context),
                bind_workspace_tool(WriteStdinTool, context),
                bind_workspace_tool(PythonExecTool, context),
            )
        )


class WebCapability(BaseCapability):
    """Fetch and research web content."""

    name = "web"
    description = "Fetch URLs, search the web, and research questions."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(
            tools=(
                bind_tool(WebFetchTool, context),
                bind_tool(WebSearchTool, context),
                bind_tool(WebResearchTool, context),
            )
        )


class McpCapability(BaseCapability):
    """Expose configured MCP servers through a compact tool facade."""

    name = "mcp"
    description = "Inspect and call configured MCP servers through compact tools."

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(tools=register_mcp_tools(self._manager(context)))

    def _manager(self, context: CapabilityContext):
        from yoke.mcp import McpManager

        return McpManager.from_paths(
            root=context.root,
            home=context.home,
            session_policy=getattr(context.provider, "_yoke_mcp_session_policy", None),
        )


class ImageInputCapability(BaseCapability):
    """Attach local images when the active model can consume image inputs."""

    name = "image.input"
    description = "Attach local images to the conversation context."

    def is_available(self, context: CapabilityContext) -> bool:
        if getattr(context.provider, "provider_name", None) == "unavailable":
            return True
        model_support = getattr(context.model, "supports_image_inputs", None)
        if isinstance(model_support, bool):
            return model_support
        return provider_supports_image_inputs(context.provider) is not False

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(
            tools=(bind_workspace_tool(AttachImageTool, context),)
        )


class ImageGenerationCapability(BaseCapability):
    """Generate images when the active provider supports it."""

    name = "image.generation"
    description = "Generate images through the active provider."

    def is_available(self, context: CapabilityContext) -> bool:
        return provider_supports_image_generation(context.provider)

    def register(self, context: CapabilityContext) -> CapabilityRegistration:
        return CapabilityRegistration(
            tools=(bind_workspace_tool(ImageGenerationTool, context),)
        )


DEFAULT_CAPABILITIES: tuple[BaseCapability, ...] = (
    FileReadCapability(),
    FileEditCapability(),
    FileSearchCapability(),
    CommandExecutionCapability(),
    FileContextCapability(),
    ImageInputCapability(),
    ImageGenerationCapability(),
    WebCapability(),
    McpCapability(),
)


def default_capabilities() -> tuple[BaseCapability, ...]:
    """Return the default built-in capability set."""
    return DEFAULT_CAPABILITIES


def research_capabilities() -> tuple[BaseCapability, ...]:
    """Return read-only capabilities useful for research agents."""
    return (
        FileReadCapability(),
        FileContextCapability(),
        FileSearchCapability(),
        ImageInputCapability(),
        WebCapability(),
    )


def worker_capabilities() -> tuple[BaseCapability, ...]:
    """Return capabilities useful for implementation agents."""
    return (
        FileReadCapability(),
        FileEditCapability(),
        FileSearchCapability(),
        FileContextCapability(),
        ImageInputCapability(),
        CommandExecutionCapability(),
        WebCapability(),
    )


def resolve_builtin_capabilities(
    context: CapabilityContext,
    capabilities: Sequence[BaseCapability] = DEFAULT_CAPABILITIES,
) -> CapabilityRegistration:
    """Resolve built-in capabilities and flatten their registration."""
    registrations = [
        capability.register(context)
        for capability in capabilities
        if capability.is_available(context)
    ]
    return CapabilityRegistration(
        tools=tuple(
            tool for registration in registrations for tool in registration.tools
        ),
        system_messages=tuple(
            message.model_copy(deep=True)
            for registration in registrations
            for message in registration.system_messages
        ),
    )


def model_prefers_apply_patch(model_id: str | None) -> bool:
    """Return whether the model should receive the apply-patch interface."""
    return isinstance(model_id, str) and "gpt" in model_id.lower()
