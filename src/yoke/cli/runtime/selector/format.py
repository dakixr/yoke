"""Formatting primitives for selector UIs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SelectorTableColumns:
    """Column labels and widths for a selector table."""

    headers: tuple[str, ...]
    widths: tuple[int, ...]

    @property
    def total(self) -> int:
        """Return the rendered table width including inter-column spacing."""
        if not self.widths:
            return 0
        return sum(self.widths) + (2 * (len(self.widths) - 1))


@dataclass(slots=True)
class GenericSelectorView:
    """Rendered view state for a generic selector."""

    lines: list[str]
    selected_line_index: int
    scroll_offset: int


def fit_selector_cell(text: str, width: int) -> str:
    """Fit text into a fixed-width cell, truncating with ellipsis if needed."""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def fit_selector_identifier(text: str, width: int) -> str:
    """Fit an identifier by preserving both its beginning and ending."""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 3:
        return text[:width]
    right_width = max(1, (width - 3) // 2)
    left_width = max(1, width - 3 - right_width)
    return text[:left_width] + "..." + text[-right_width:]


def truncate_selector_line(
    text: str,
    width: int,
    *,
    preserve_end: bool = False,
) -> str:
    """Truncate a rendered selector line to the visible terminal width."""
    if len(text) <= width:
        return text
    if preserve_end:
        return fit_selector_identifier(text, width)
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def format_selector_table_header(columns: SelectorTableColumns) -> str:
    """Format a selector table header row."""
    parts = [
        fit_selector_cell(header, width)
        for header, width in zip(columns.headers, columns.widths, strict=False)
    ]
    return "  ".join(parts)


def format_selector_table_separator(columns: SelectorTableColumns) -> str:
    """Format a selector table separator line."""
    return "-" * max(1, columns.total)


def generic_selector_footer(
    *,
    scroll_offset: int,
    visible_count: int,
    item_count: int,
    footer: str,
) -> str:
    """Format the selector footer with the current visible range."""
    visible_start = scroll_offset + 1
    visible_end = scroll_offset + visible_count
    return f"Showing {visible_start}-{visible_end} of {item_count}. {footer}"


def normalize_selector_scroll_offset(
    *,
    selected_index: int,
    scroll_offset: int,
    item_count: int,
    body_rows: int,
) -> int:
    """Keep the selected row inside the visible viewport."""
    max_offset = max(0, item_count - body_rows)
    scroll_offset = max(0, min(scroll_offset, max_offset))
    if selected_index < scroll_offset:
        return selected_index
    if selected_index >= scroll_offset + body_rows:
        return selected_index - body_rows + 1
    return scroll_offset
