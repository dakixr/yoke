"""Tool cloning helpers for isolated agent turns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.agent.loop.types import ConversationEntryHistory
from yoke.agent.tools import LocalTool

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent


def copy_tool_for_fork(tool: LocalTool) -> LocalTool:
    """Copy a tool without sharing mutable per-turn runtime context."""
    copied = tool.model_copy(deep=False)
    copied._context = {
        key: value
        for key, value in tool._context.items()
        if key not in {"command_process_manager", "runtime_context"}
    }
    return copied


def promote_runtime_fork(primary: RuntimeAgent, forked: RuntimeAgent) -> None:
    """Promote a completed turn and leave displaced resources on its old owner."""
    previous_provider = primary.provider
    primary.provider = forked.provider
    forked.provider = previous_provider
    primary.load_conversation(
        ConversationEntryHistory(forked.conversation_entries),
        available_skills=forked.available_skills,
        active_skills=forked.active_skills,
    )
    primary.refresh_tools(force=True)
