"""Shared interactive selector UI primitives."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from contextlib import suppress
from html import escape
import shutil
from typing import TypeVar

from yoke.cli.runtime.selector.format import (
    format_selector_table_header,
)
from yoke.cli.runtime.selector.format import (
    format_selector_table_separator,
)
from yoke.cli.runtime.selector.format import generic_selector_footer
from yoke.cli.runtime.selector.format import GenericSelectorView
from yoke.cli.runtime.selector.format import (
    normalize_selector_scroll_offset,
)
from yoke.cli.runtime.selector.format import SelectorTableColumns
from yoke.cli.runtime.selector.format import truncate_selector_line
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)

ItemT = TypeVar("ItemT")


def can_use_keyboard_selector(stream: object) -> bool:
    """Return whether the provided stream supports TTY keyboard selection."""
    is_tty = getattr(stream, "isatty", None)
    return bool(is_tty and is_tty())


def select_list_item_interactive(
    items: Sequence[ItemT],
    *,
    title: str,
    subtitle: str | None = None,
    render_item: Callable[[ItemT, int, bool, int], str],
    footer: str,
) -> ItemT | None:
    """Render a keyboard-driven selector for an arbitrary list of items."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not items:
        return None

    selected_index = 0
    scroll_offset = 0

    def formatted_rows() -> HTML:
        nonlocal scroll_offset
        terminal_columns, terminal_rows = selector_terminal_size()
        view = build_generic_selector_view(
            items,
            selected_index=selected_index,
            scroll_offset=scroll_offset,
            terminal_columns=terminal_columns,
            terminal_rows=terminal_rows,
            title=title,
            subtitle=subtitle,
            render_item=render_item,
            footer=footer,
        )
        scroll_offset = view.scroll_offset
        rendered_lines: list[str] = []
        for index, line in enumerate(view.lines):
            escaped_line = escape(line)
            if index == view.selected_line_index:
                rendered_lines.append(f"<reverse>{escaped_line}</reverse>")
            else:
                rendered_lines.append(escaped_line)
        return HTML("\n".join(rendered_lines))

    key_bindings = KeyBindings()
    app: Application[ItemT | None]

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event) -> None:
        nonlocal selected_index
        selected_index = min(selected_index + 1, len(items) - 1)
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event) -> None:
        nonlocal selected_index
        selected_index = max(selected_index - 1, 0)
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        nonlocal selected_index
        selected_index = min(
            selected_index + selector_page_step(),
            len(items) - 1,
        )
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        nonlocal selected_index
        selected_index = max(selected_index - selector_page_step(), 0)
        event.app.invalidate()

    @key_bindings.add("home")
    def _move_home(event) -> None:
        nonlocal selected_index
        selected_index = 0
        event.app.invalidate()

    @key_bindings.add("end")
    def _move_end(event) -> None:
        nonlocal selected_index
        selected_index = len(items) - 1
        event.app.invalidate()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=items[selected_index])

    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    @key_bindings.add("q")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    control = FormattedTextControl(formatted_rows, focusable=True)
    app = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=key_bindings,
        full_screen=False,
        mouse_support=False,
    )
    with suppress(EOFError, KeyboardInterrupt):
        return app.run()
    return None


def select_table_item_interactive(
    items: Sequence[ItemT],
    *,
    title: str,
    subtitle: str | None = None,
    columns: SelectorTableColumns,
    render_row: Callable[[ItemT, int, bool, SelectorTableColumns], str],
    footer: str,
) -> ItemT | None:
    """Render a keyboard-driven selector for rows arranged as a table."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not items:
        return None

    selected_index = 0
    scroll_offset = 0

    def formatted_rows() -> HTML:
        nonlocal scroll_offset
        terminal_columns, terminal_rows = selector_terminal_size()
        view = build_table_selector_view(
            items,
            selected_index=selected_index,
            scroll_offset=scroll_offset,
            terminal_columns=terminal_columns,
            terminal_rows=terminal_rows,
            title=title,
            subtitle=subtitle,
            columns=columns,
            render_row=render_row,
            footer=footer,
        )
        scroll_offset = view.scroll_offset
        rendered_lines: list[str] = []
        for index, line in enumerate(view.lines):
            escaped_line = escape(line)
            if index == view.selected_line_index:
                rendered_lines.append(f"<reverse>{escaped_line}</reverse>")
            else:
                rendered_lines.append(escaped_line)
        return HTML("\n".join(rendered_lines))

    key_bindings = KeyBindings()
    app: Application[ItemT | None]

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event) -> None:
        nonlocal selected_index
        selected_index = min(selected_index + 1, len(items) - 1)
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event) -> None:
        nonlocal selected_index
        selected_index = max(selected_index - 1, 0)
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        nonlocal selected_index
        selected_index = min(
            selected_index + selector_page_step(),
            len(items) - 1,
        )
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        nonlocal selected_index
        selected_index = max(selected_index - selector_page_step(), 0)
        event.app.invalidate()

    @key_bindings.add("home")
    def _move_home(event) -> None:
        nonlocal selected_index
        selected_index = 0
        event.app.invalidate()

    @key_bindings.add("end")
    def _move_end(event) -> None:
        nonlocal selected_index
        selected_index = len(items) - 1
        event.app.invalidate()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=items[selected_index])

    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    @key_bindings.add("q")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    control = FormattedTextControl(formatted_rows, focusable=True)
    app = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=False,
    )
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            return app.run()
    return None


def build_generic_selector_view(
    items: Sequence[ItemT],
    *,
    selected_index: int,
    scroll_offset: int,
    terminal_columns: int,
    terminal_rows: int,
    title: str,
    subtitle: str | None,
    render_item: Callable[[ItemT, int, bool, int], str],
    footer: str,
) -> GenericSelectorView:
    """Build a rendered generic selector view."""
    columns = max(20, terminal_columns)
    subtitle_lines = subtitle.splitlines() if subtitle else []
    header_lines = 1 + len(subtitle_lines)
    body_rows = max(1, max(6, terminal_rows) - (header_lines + 3))
    selected_index = max(0, min(selected_index, len(items) - 1))
    scroll_offset = normalize_selector_scroll_offset(
        selected_index=selected_index,
        scroll_offset=scroll_offset,
        item_count=len(items),
        body_rows=body_rows,
    )
    visible_items = items[scroll_offset : scroll_offset + body_rows]
    lines = [truncate_selector_line(title, columns)]
    for subtitle_line in subtitle_lines:
        lines.append(
            truncate_selector_line(
                subtitle_line,
                columns,
                preserve_end=True,
            )
        )
    lines.append("")
    for index, item in enumerate(visible_items, start=scroll_offset):
        lines.append(
            truncate_selector_line(
                render_item(item, index, index == selected_index, columns),
                columns,
            )
        )
    lines.append("")
    lines.append(
        truncate_selector_line(
            generic_selector_footer(
                scroll_offset=scroll_offset,
                visible_count=len(visible_items),
                item_count=len(items),
                footer=footer,
            ),
            columns,
        )
    )
    return GenericSelectorView(
        lines=lines,
        selected_line_index=header_lines + 1 + (selected_index - scroll_offset),
        scroll_offset=scroll_offset,
    )


def build_table_selector_view(
    items: Sequence[ItemT],
    *,
    selected_index: int,
    scroll_offset: int,
    terminal_columns: int,
    terminal_rows: int,
    title: str,
    subtitle: str | None,
    columns: SelectorTableColumns,
    render_row: Callable[[ItemT, int, bool, SelectorTableColumns], str],
    footer: str,
) -> GenericSelectorView:
    """Build a rendered table selector view."""
    if columns.headers:
        header_text = format_selector_table_header(columns)
        separator_text = format_selector_table_separator(columns)
        subtitle = (
            f"{subtitle}\n{header_text}\n{separator_text}"
            if subtitle
            else f"{header_text}\n{separator_text}"
        )
    return build_generic_selector_view(
        items,
        selected_index=selected_index,
        scroll_offset=scroll_offset,
        terminal_columns=terminal_columns,
        terminal_rows=terminal_rows,
        title=title,
        subtitle=subtitle,
        render_item=lambda item, index, is_selected, terminal_width: (
            render_table_selector_row(
                item,
                index=index,
                is_selected=is_selected,
                terminal_columns=terminal_width,
                columns=columns,
                render_row=render_row,
            )
        ),
        footer=footer,
    )


def render_table_selector_row(
    item: ItemT,
    *,
    index: int,
    is_selected: bool,
    terminal_columns: int,
    columns: SelectorTableColumns,
    render_row: Callable[[ItemT, int, bool, SelectorTableColumns], str],
) -> str:
    """Render one row in a table selector."""
    del terminal_columns
    return render_row(item, index, is_selected, columns)


def selector_page_step() -> int:
    """Return the number of rows advanced by page-up/page-down."""
    return max(1, selector_terminal_size()[1] - 6)


def selector_terminal_size() -> tuple[int, int]:
    """Return the current terminal size with prompt-toolkit awareness."""
    with suppress(Exception):
        from prompt_toolkit.application.current import get_app_or_none

        app = get_app_or_none()
        if app is not None:
            size = app.output.get_size()
            return size.columns, size.rows
    size = shutil.get_terminal_size(fallback=(100, 24))
    return size.columns, size.lines
