"""Helpers for isolated interactive turns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.agent.tools import LocalTool

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent


def copy_tool_for_runtime(tool: LocalTool) -> LocalTool:
    """Copy a caller-provided tool before binding it to one runtime."""
    copied = tool.model_copy(deep=False)
    copied._context = dict(tool._context)
    return copied


def copy_tool_for_fork(tool: LocalTool) -> LocalTool:
    """Copy a tool without deep-copying process and network resources."""
    copied = tool.model_copy(deep=False)
    copied._context = {
        key: value
        for key, value in tool._context.items()
        if key not in {"runtime_context", "provider", "model", "cancel_requested"}
    }
    return copied


def promote_runtime_fork(primary: RuntimeAgent, forked: RuntimeAgent) -> None:
    """Promote one accepted turn while leaving old resources on the fork."""
    previous_provider = primary.provider
    primary.provider = forked.provider
    forked.provider = previous_provider
    primary._context = forked._context
    primary._context_owned_for_run = False
    forked._context = None
    primary._seen_command_completion_events.clear()
    primary._seen_command_completion_events.update(
        forked._seen_command_completion_events
    )
    primary._seen_dropped_completion_events = forked._seen_dropped_completion_events
    primary.active_skills = [
        skill.model_copy(deep=True) for skill in forked.active_skills
    ]
    if primary.refresh_tools(force=True) and primary._context is not None:
        primary._sync_context_instructions(primary._context)
