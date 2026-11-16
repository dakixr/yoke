"""Owned display state for the inspector."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from yoke.cli.list_cursor import ListCursor
from yoke.agent.tools.command_process.types import CommandProcessSnapshot
from yoke.cli.interactive.process_inspector.render import DetailOutputCache


@dataclass(slots=True)
class ProcessInspectorState:
    """Mutable UI state for the process inspector."""

    processes: list[CommandProcessSnapshot]
    cursor: ListCursor = field(default_factory=ListCursor)
    detail_scroll: int = 0
    wrap: bool = True
    notice: str = ""
    active_pane: Literal["sidebar", "detail"] = "sidebar"
    detail_output_cache: DetailOutputCache = field(default_factory=DetailOutputCache)

    def __post_init__(self) -> None:
        """Start with the newest retained process selected."""
        if self.processes:
            self.cursor.index = len(self.processes) - 1
