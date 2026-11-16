"""Owned display state for the inspector."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from yoke.cli.list_cursor import ListCursor
from yoke.cli.interactive.tool_inspector.trace import ToolTraceEntry
from yoke.cli.interactive.tool_inspector.render import sidebar_items


@dataclass(slots=True)
class ToolInspectorState:
    """Mutable UI state for the tool inspector."""

    entries: list[ToolTraceEntry]
    cursor: ListCursor = field(default_factory=ListCursor)
    detail_scroll: int = 0
    search: str = ""
    searching: bool = False
    raw: bool = False
    wrap: bool = True
    notice: str = ""
    active_pane: Literal["sidebar", "detail"] = "sidebar"
    data_revision: int = 0
    source_revision: object | None = None
    detail_cache_key: tuple[object, ...] | None = field(default=None, repr=False)
    detail_cache_lines: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        """Start on the newest sidebar item by default."""
        items = sidebar_items(self.entries)
        if items:
            self.cursor.index = len(items) - 1
