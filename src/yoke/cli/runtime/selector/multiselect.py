"""Fullscreen multi-select selector helpers."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from contextlib import suppress
from html import escape
from typing import TypeVar

from yoke.cli.runtime.selector.ui import build_table_selector_view
from yoke.cli.runtime.selector.ui import selector_page_step
from yoke.cli.runtime.selector.ui import SelectorTableColumns
from yoke.cli.runtime.selector.ui import selector_terminal_size
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)

ItemT = TypeVar("ItemT")


def select_table_items_interactive(
    items: Sequence[ItemT],
    *,
    title: str,
    subtitle: str | None = None,
    columns: SelectorTableColumns,
    render_row: Callable[[ItemT, int, bool, bool, SelectorTableColumns], str],
    footer: str,
    selected_indexes: set[int] | None = None,
) -> set[int] | None:
    """Render a fullscreen multi-select table and return selected indexes."""
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not items:
        return set()

    selected_index = 0
    scroll_offset = 0
    active_indexes = set(selected_indexes or set())

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
            render_row=(
                lambda item, index, is_cursor, columns: render_row(
                    item,
                    index,
                    is_cursor,
                    index in active_indexes,
                    columns,
                )
            ),
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

    def get_selected_index() -> int:
        return selected_index

    def set_selected_index(value: int) -> None:
        nonlocal selected_index
        selected_index = value

    key_bindings = _build_multiselect_key_bindings(
        item_count=len(items),
        active_indexes=active_indexes,
        get_selected_index=get_selected_index,
        set_selected_index=set_selected_index,
    )
    control = FormattedTextControl(formatted_rows, focusable=True)
    return _run_multiselect_application(control, key_bindings)


def _build_multiselect_key_bindings(
    *,
    item_count: int,
    active_indexes: set[int],
    get_selected_index: Callable[[], int],
    set_selected_index: Callable[[int], None],
) -> object:
    from prompt_toolkit.key_binding import KeyBindings

    key_bindings = KeyBindings()

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event) -> None:
        set_selected_index(min(get_selected_index() + 1, item_count - 1))
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event) -> None:
        set_selected_index(max(get_selected_index() - 1, 0))
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        set_selected_index(
            min(get_selected_index() + selector_page_step(), item_count - 1)
        )
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        set_selected_index(max(get_selected_index() - selector_page_step(), 0))
        event.app.invalidate()

    @key_bindings.add("home")
    def _move_home(event) -> None:
        set_selected_index(0)
        event.app.invalidate()

    @key_bindings.add("end")
    def _move_end(event) -> None:
        set_selected_index(item_count - 1)
        event.app.invalidate()

    @key_bindings.add(" ")
    def _toggle(event) -> None:
        selected_index = get_selected_index()
        if selected_index in active_indexes:
            active_indexes.remove(selected_index)
        else:
            active_indexes.add(selected_index)
        event.app.invalidate()

    @key_bindings.add("a")
    def _enable_all(event) -> None:
        active_indexes.update(range(item_count))
        event.app.invalidate()

    @key_bindings.add("d")
    def _disable_all(event) -> None:
        active_indexes.clear()
        event.app.invalidate()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=set(active_indexes))

    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    @key_bindings.add("q")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    return key_bindings


def _run_multiselect_application(control, key_bindings) -> set[int] | None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window

    app: Application[set[int] | None] = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=False,
    )
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            return app.run()
    return None
