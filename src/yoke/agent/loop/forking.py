"""Helpers for isolated interactive turns."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
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
    primary.promote(forked)


def bound_tool_path(tools: Sequence[LocalTool], key: str) -> Path | None:
    """Return a bound path-like tool context value from a tool sequence."""
    for tool in tools:
        value = tool._context.get(key)
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value)
    return None
