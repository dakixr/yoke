"""Process launch dependency and compatibility handle allocator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.agent.tools.command_process.spawn import open_process as open_process

if TYPE_CHECKING:
    from yoke.agent.tools.command_process.manager import CommandProcessManager


def allocate_session_id(manager: CommandProcessManager) -> int:
    """Allocate through the owner, preserving the historical helper import."""
    return manager.allocate_session_id()
