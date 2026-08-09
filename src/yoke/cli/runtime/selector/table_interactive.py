"""Interactive table selector extensions."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from html import escape
from typing import TypeVar
from typing import cast

from yoke.cli.runtime.selector.format import GenericSelectorView
from yoke.cli.runtime.selector.format import SelectorTableColumns
from yoke.cli.runtime.selector.format import (
    format_selector_table_header,
)
from yoke.cli.runtime.selector.format import (
    format_selector_table_separator,
)
from yoke.cli.runtime.selector.format import truncate_selector_line
from yoke.cli.runtime.selector.table_state import _KeyBindingsProtocol
from yoke.cli.runtime.selector.table_state import _TableSelectorState
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)

ItemT = TypeVar("ItemT")


def select_table_item_interactive_impl(
    items: Sequence[ItemT],
    *,
    title: str,
    subtitle: str | None = None,
    columns: SelectorTableColumns,
    render_row: Callable[[ItemT, int, bool, SelectorTableColumns], str],
    footer: str,
    filter_item: Callable[[ItemT, str], bool] | None = None,
    filter_label: str = "Search",
    action_key: str | None = None,
    action_label: str | None = None,
    on_action: Callable[[ItemT], None] | None = None,
) -> ItemT | None:
    """Render a keyboard-driven selector for rows arranged as a table."""
    from contextlib import suppress

    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not items:
        return None

    state = _TableSelectorState(
        items,
        filter_item=filter_item,
        filter_label=filter_label,
        footer=footer,
        subtitle=subtitle,
        action_key=action_key,
        action_label=action_label,
        on_action=on_action,
    )

    def formatted_rows() -> HTML:
        return HTML(
            _format_table_selector_rows(
                state,
                title=title,
                columns=columns,
                render_row=render_row,
            )
        )

    key_bindings = KeyBindings()
    app: Application[ItemT | None]
    _register_table_selector_navigation(key_bindings, state)
    _register_table_selector_accept(key_bindings, state)
    _register_table_selector_search(key_bindings, state)
    _register_table_selector_action(key_bindings, state)
    _register_table_selector_cancel(key_bindings, state)

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


def _empty_table_selector_lines(
    *,
    terminal_columns: int,
    title: str,
    subtitle: str | None,
    columns: SelectorTableColumns,
    footer: str,
) -> list[str]:
    columns_width = max(20, terminal_columns)
    subtitle_lines = subtitle.splitlines() if subtitle else []
    lines = [truncate_selector_line(title, columns_width)]
    for subtitle_line in subtitle_lines:
        lines.append(truncate_selector_line(subtitle_line, columns_width))
    if columns.headers:
        lines.append(format_selector_table_header(columns))
        lines.append(format_selector_table_separator(columns))
    lines.extend(["", "No matching sessions.", ""])
    lines.append(truncate_selector_line(footer, columns_width))
    return lines


def _format_table_selector_rows(
    state: _TableSelectorState[ItemT],
    *,
    title: str,
    columns: SelectorTableColumns,
    render_row: Callable[[ItemT, int, bool, SelectorTableColumns], str],
) -> str:
    from yoke.cli.runtime.selector.ui import build_table_selector_view
    from yoke.cli.runtime.selector.ui import selector_terminal_size

    terminal_columns, terminal_rows = selector_terminal_size()
    current_items = state.filtered_items()
    state.normalize_selected_index(current_items)
    current_footer = state.current_footer()
    if not current_items:
        lines = _empty_table_selector_lines(
            terminal_columns=terminal_columns,
            title=title,
            subtitle=state.visible_subtitle(),
            columns=columns,
            footer=current_footer,
        )
        return "\n".join(escape(line) for line in lines)
    view = build_table_selector_view(
        current_items,
        selected_index=state.selected_index,
        scroll_offset=state.scroll_offset,
        terminal_columns=terminal_columns,
        terminal_rows=terminal_rows,
        title=title,
        subtitle=state.visible_subtitle(),
        columns=columns,
        render_row=render_row,
        footer=current_footer,
    )
    state.scroll_offset = view.scroll_offset
    return _format_selector_view_lines(view)


def _format_selector_view_lines(view: GenericSelectorView) -> str:
    rendered_lines: list[str] = []
    for index, line in enumerate(view.lines):
        escaped_line = escape(line)
        if index == view.selected_line_index:
            rendered_lines.append(f"<reverse>{escaped_line}</reverse>")
        else:
            rendered_lines.append(escaped_line)
    return "\n".join(rendered_lines)


def _register_table_selector_navigation[ItemT](
    key_bindings: _KeyBindingsProtocol,
    state: _TableSelectorState[ItemT],
) -> None:
    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event) -> None:
        if state.search_mode:
            if event.key_sequence[-1].key == "j":
                state.append_search_text("j")
            event.app.invalidate()
            return
        state.move_down()
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event) -> None:
        if state.search_mode:
            if event.key_sequence[-1].key == "k":
                state.append_search_text("k")
            event.app.invalidate()
            return
        state.move_up()
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        state.page_down()
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        state.page_up()
        event.app.invalidate()

    @key_bindings.add("home")
    def _move_home(event) -> None:
        state.move_home()
        event.app.invalidate()

    @key_bindings.add("end")
    def _move_end(event) -> None:
        state.move_end()
        event.app.invalidate()


def _register_table_selector_accept[ItemT](
    key_bindings: _KeyBindingsProtocol,
    state: _TableSelectorState[ItemT],
) -> None:
    @key_bindings.add("enter")
    def _accept(event) -> None:
        if state.search_mode:
            state.search_mode = False
            event.app.invalidate()
            return
        selected_item = state.selected_item()
        if selected_item is not None:
            event.app.exit(result=selected_item)


def _register_table_selector_search[ItemT](
    key_bindings: _KeyBindingsProtocol,
    state: _TableSelectorState[ItemT],
) -> None:
    from prompt_toolkit.keys import Keys

    @key_bindings.add("/")
    def _start_search(event) -> None:
        if state.filter_item is None:
            return
        state.search_mode = True
        event.app.invalidate()

    @key_bindings.add("backspace")
    def _search_backspace(event) -> None:
        if not state.search_mode:
            return
        state.query = state.query[:-1]
        event.app.invalidate()

    @key_bindings.add(cast(str, Keys.Any))
    def _search_any(event) -> None:
        if state.search_mode:
            data = event.data
            if data and data.isprintable():
                state.append_search_text(data)
            event.app.invalidate()


def _register_table_selector_action[ItemT](
    key_bindings: _KeyBindingsProtocol,
    state: _TableSelectorState[ItemT],
) -> None:
    if not state.action_key or state.on_action is None:
        return

    @key_bindings.add(state.action_key)
    def _run_action(event) -> None:
        if state.search_mode:
            state.append_search_text(state.action_key or "")
            event.app.invalidate()
            return
        state.run_action()
        event.app.invalidate()


def _register_table_selector_cancel[ItemT](
    key_bindings: _KeyBindingsProtocol,
    state: _TableSelectorState[ItemT],
) -> None:
    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    @key_bindings.add("q")
    def _cancel(event) -> None:
        key = event.key_sequence[-1].key
        if state.search_mode and key != "c-c":
            if key == "q":
                state.append_search_text("q")
            else:
                state.query = ""
                state.search_mode = False
            event.app.invalidate()
            return
        event.app.exit(result=None)
