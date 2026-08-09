"""Interactive selector helpers for `yoke resume`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from yoke.cli.path_display import format_root_label
from yoke.cli.runtime.selector.ui import can_use_keyboard_selector
from yoke.cli.runtime.selector.format import fit_selector_cell
from yoke.cli.runtime.selector.format import fit_selector_identifier
from yoke.cli.runtime.selector.ui import SelectorTableColumns
from yoke.cli.runtime.selector.ui import selector_terminal_size
from yoke.cli.runtime.selector.ui import select_table_item_interactive
from yoke.cli.session import SessionRecord


@dataclass(slots=True)
class _SessionSelectorColumnWidths:
    index: int
    pin: int
    title: int
    updated: int
    root: int
    session_id: int

    @property
    def total(self) -> int:
        return (
            self.index
            + self.pin
            + self.title
            + self.updated
            + self.root
            + self.session_id
            + 12
        )


def _can_use_keyboard_session_selector(stream: object) -> bool:
    return can_use_keyboard_selector(stream)


def _select_session_id_interactive(
    records: list[SessionRecord],
    *,
    root: Path,
    all_sessions: bool = False,
    set_pinned: Callable[[str, bool], SessionRecord] | None = None,
) -> str | None:
    _sort_session_selector_records(records)
    terminal_columns = selector_terminal_size()[0]
    widths = _session_selector_column_widths(
        records,
        terminal_columns=max(20, terminal_columns),
        include_root=all_sessions,
    )
    table_columns = _session_table_columns(widths, include_root=all_sessions)
    selected = select_table_item_interactive(
        records,
        title="Select a session to resume:",
        subtitle=("All workspace roots" if all_sessions else f"Root: {root.resolve()}"),
        columns=table_columns,
        render_row=lambda record, index, is_selected, _columns: (
            _format_session_selector_row(
                record,
                index=index,
                widths=widths,
                is_selected=is_selected,
                include_root=all_sessions,
            )
        ),
        footer=(
            "Use Up/Down or j/k, PgUp/PgDn, Home/End, Enter to resume, q to cancel."
        ),
        filter_item=_session_title_matches_query,
        filter_label="Title search",
        action_key="p" if set_pinned is not None else None,
        action_label="pin/unpin" if set_pinned is not None else None,
        on_action=(
            (lambda record: _toggle_session_pin(records, record, set_pinned))
            if set_pinned is not None
            else None
        ),
    )
    if selected is None:
        return None
    if isinstance(selected, SessionRecord):
        return selected.id
    return str(selected)


def _session_table_columns(
    widths: _SessionSelectorColumnWidths,
    *,
    include_root: bool,
) -> SelectorTableColumns:
    headers = ["#", "Pin", "Title", "Updated"]
    column_widths = [
        widths.index + 2,
        widths.pin,
        widths.title,
        widths.updated,
    ]
    if include_root:
        headers.append("Root")
        column_widths.append(widths.root)
    headers.append("Session ID")
    column_widths.append(widths.session_id)
    return SelectorTableColumns(
        headers=tuple(headers),
        widths=tuple(column_widths),
    )


def _format_session_selector_row(
    record: SessionRecord,
    *,
    index: int,
    widths: _SessionSelectorColumnWidths,
    is_selected: bool,
    include_root: bool,
) -> str:
    table_columns = _session_table_columns(widths, include_root=include_root)
    marker = ">" if is_selected else " "
    cells = [
        _fit_session_selector_cell(
            f"{marker} {index + 1:>{widths.index}}",
            table_columns.widths[0],
        ),
        fit_selector_cell(
            "★" if record.pinned else "",
            table_columns.widths[1],
        ),
        fit_selector_cell(
            record.title or "Untitled session",
            table_columns.widths[2],
        ),
        fit_selector_cell(
            _format_session_activity(record),
            table_columns.widths[3],
        ),
    ]
    next_index = 4
    if include_root:
        cells.append(
            fit_selector_identifier(
                _format_session_root(record),
                table_columns.widths[next_index],
            )
        )
        next_index += 1
    cells.append(
        fit_selector_identifier(
            record.id,
            table_columns.widths[next_index],
        )
    )
    return "  ".join(cells)


def _session_selector_column_widths(
    records: list[SessionRecord],
    *,
    terminal_columns: int,
    include_root: bool,
) -> _SessionSelectorColumnWidths:
    index_width = max(1, len(str(len(records))))
    updated_lengths = [len(_format_session_activity(record)) for record in records]
    updated_width = max(
        len("Updated"),
        max(updated_lengths, default=0),
    )
    available = max(
        8,
        terminal_columns
        - index_width
        - len("Pin")
        - updated_width
        - (14 if include_root else 12),
    )
    session_id_width = min(
        max(
            len("Session ID"),
            max((len(record.id) for record in records), default=0),
        ),
        max(4, available // (4 if include_root else 3)),
    )
    root_width = 0
    if include_root:
        root_width = min(
            max(
                len("Root"),
                max(
                    (len(_format_session_root(record)) for record in records),
                    default=0,
                ),
            ),
            max(8, available // 3),
        )
    return _SessionSelectorColumnWidths(
        index=index_width,
        pin=len("Pin"),
        title=max(1, available - session_id_width - root_width),
        updated=updated_width,
        root=root_width,
        session_id=session_id_width,
    )


def _sort_session_selector_records(records: list[SessionRecord]) -> None:
    records.sort(
        key=lambda record: (
            record.pinned,
            record.updated_at or record.created_at or "",
        ),
        reverse=True,
    )


def _toggle_session_pin(
    records: list[SessionRecord],
    record: SessionRecord,
    set_pinned: Callable[[str, bool], SessionRecord] | None,
) -> None:
    if set_pinned is None:
        return
    updated = set_pinned(record.id, not record.pinned)
    for index, current in enumerate(records):
        if current.id == updated.id:
            records[index] = updated
            break
    _sort_session_selector_records(records)


def _session_title_matches_query(record: SessionRecord, query: str) -> bool:
    return _fuzzy_match(query, record.title or "Untitled session")


def _fuzzy_match(query: str, text: str) -> bool:
    normalized_query = "".join(query.lower().split())
    if not normalized_query:
        return True
    normalized_text = text.lower()
    if query.lower().strip() in normalized_text:
        return True
    search_from = 0
    for character in normalized_query:
        found_at = normalized_text.find(character, search_from)
        if found_at < 0:
            return False
        search_from = found_at + 1
    return True


def _fit_session_selector_cell(text: str, width: int) -> str:
    return fit_selector_cell(text, width)


def _format_session_root(record: SessionRecord) -> str:
    if not record.root:
        return "-"
    return format_root_label(Path(record.root))


def _format_session_activity(record: SessionRecord) -> str:
    timestamp = record.updated_at or record.created_at
    parsed = _parse_session_timestamp(timestamp)
    if parsed is None:
        return "unknown time"
    age = datetime.now(UTC) - parsed
    if age.days >= 1:
        return f"{age.days}d ago"
    hours = age.seconds // 3600
    if hours >= 1:
        return f"{hours}h ago"
    minutes = age.seconds // 60
    if minutes >= 1:
        return f"{minutes}m ago"
    return "just now"


def _parse_session_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
