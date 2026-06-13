"""Model-aware selection for the built-in file writing capability."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.tools.apply_patch import ApplyPatchTool
from yoke.agent.tools.context import ToolRegistrationContext
from yoke.agent.tools.context import ToolRegistrationResult
from yoke.agent.tools.edit import EditTool

_TOOLS_DIR = Path(__file__).parent
APPLY_PATCH_SYSTEM_PROMPT = (
    (_TOOLS_DIR / "apply_patch" / "prompt.md").read_text(encoding="utf-8").strip()
)
EDIT_SYSTEM_PROMPT = (_TOOLS_DIR / "edit_prompt.md").read_text(encoding="utf-8").strip()


def register_write_tool(context: ToolRegistrationContext) -> ToolRegistrationResult:
    """Register the preferred writing tool for the selected model."""
    prefers_patch = model_prefers_apply_patch(context.model_id)
    tool_class = ApplyPatchTool if prefers_patch else EditTool
    system_prompt = APPLY_PATCH_SYSTEM_PROMPT if prefers_patch else EDIT_SYSTEM_PROMPT
    bind_context: dict[str, object] = {
        "root": context.root,
        "provider": context.provider,
    }
    if context.home is not None:
        bind_context["home"] = context.home
    if context.cancel_requested is not None:
        bind_context["cancel_requested"] = context.cancel_requested
    return ToolRegistrationResult(
        tools=[tool_class.bind(**bind_context)],
        system_messages=[Message.system(system_prompt)],
    )


def model_prefers_apply_patch(model_id: str | None) -> bool:
    """Return whether the model should receive the apply-patch interface."""
    return isinstance(model_id, str) and "gpt" in model_id.lower()
