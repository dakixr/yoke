"""Fullscreen prompt-toolkit inspector for tool call traces."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal
from yoke.cli.interactive.tool_trace import ToolTraceEntry
from yoke.cli.interactive.tool_inspector_render import detail_text
from yoke.cli.interactive.tool_inspector_render import entry_text
from yoke.cli.interactive.tool_inspector_render import move_selection
from yoke.cli.interactive.tool_inspector_render import page_step
from yoke.cli.interactive.tool_inspector_render import render_view_html
from yoke.cli.interactive.tool_inspector_render import selected_entry
from yoke.cli.interactive.tool_inspector_render import sidebar_items


@dataclass(slots=True)
class ToolInspectorState:
    """Mutable UI state for the tool inspector."""

    entries: list[ToolTraceEntry]
    selected_index: int = 0
    list_scroll: int = 0
    detail_scroll: int = 0
    search: str = ""
    searching: bool = False
    raw: bool = False
    wrap: bool = True
    notice: str = ""
    active_pane: Literal["sidebar", "detail"] = "sidebar"

    def __post_init__(self) -> None:
        """Start on the newest sidebar item by default."""
        items = sidebar_items(self.entries)
        if items:
            self.selected_index = len(items) - 1


def open_tool_inspector(entries: Sequence[ToolTraceEntry]) -> None:
    """Open a fullscreen alternate-buffer view of tool calls."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    state = ToolInspectorState(entries=list(entries))
    key_bindings = KeyBindings()

    def visible_entries():
        query = state.search.strip().lower()
        items = sidebar_items(state.entries)
        if not query:
            return items
        return [entry for entry in items if query in entry_text(entry)]

    def formatted_rows() -> HTML:
        visible = visible_entries()
        return HTML(render_view_html(state, visible))
    _register_tool_inspector_keys(
        key_bindings,
        state=state,
        visible_entries=visible_entries,
        any_key=Keys.Any,
    )

    control = FormattedTextControl(formatted_rows, focusable=True)
    app: Application[None] = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=False,
    )
    with suppress(EOFError, KeyboardInterrupt):
        app.run()


def _register_tool_inspector_keys(
    key_bindings,
    *,
    state: ToolInspectorState,
    visible_entries,
    any_key,
) -> None:
    """Register key bindings for the tool inspector app."""
    _register_navigation_keys(key_bindings, state, visible_entries)
    _register_mode_keys(key_bindings, state)
    _register_search_keys(key_bindings, state, any_key)
    _register_copy_and_exit_keys(key_bindings, state, visible_entries)


def _register_navigation_keys(key_bindings, state, visible_entries) -> None:
    """Register selection and scroll keys."""
    from prompt_toolkit.keys import Keys

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, visible_entries(), 1)
        else:
            state.detail_scroll += 1
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, visible_entries(), -1)
        else:
            state.detail_scroll = max(0, state.detail_scroll - 1)
        event.app.invalidate()

    @key_bindings.add(Keys.ScrollDown)
    def _scroll_down(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, visible_entries(), 1)
        else:
            state.detail_scroll += 1
        event.app.invalidate()

    @key_bindings.add(Keys.ScrollUp)
    def _scroll_up(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, visible_entries(), -1)
        else:
            state.detail_scroll = max(0, state.detail_scroll - 1)
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _detail_page_down(event) -> None:
        state.detail_scroll += page_step()
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _detail_page_up(event) -> None:
        state.detail_scroll = max(0, state.detail_scroll - page_step())
        event.app.invalidate()

    @key_bindings.add("h")
    @key_bindings.add("l")
    def _toggle_pane(event) -> None:
        state.active_pane = _other_pane(state.active_pane)
        event.app.invalidate()

    @key_bindings.add("left")
    def _focus_sidebar(event) -> None:
        state.active_pane = "sidebar"
        event.app.invalidate()

    @key_bindings.add("right")
    def _focus_detail(event) -> None:
        state.active_pane = "detail"
        event.app.invalidate()

    @key_bindings.add("home")
    @key_bindings.add("g")
    def _home(event) -> None:
        if state.active_pane == "sidebar":
            state.selected_index = 0
            state.detail_scroll = 0
        else:
            state.detail_scroll = 0
        event.app.invalidate()

    @key_bindings.add("end")
    @key_bindings.add("G")
    def _end(event) -> None:
        if state.active_pane == "sidebar":
            state.selected_index = max(0, len(visible_entries()) - 1)
            state.detail_scroll = 0
        else:
            state.detail_scroll = 10**9
        event.app.invalidate()


def _register_mode_keys(key_bindings, state) -> None:
    """Register display-mode keys."""

    @key_bindings.add("r")
    def _toggle_raw(event) -> None:
        state.raw = not state.raw
        state.detail_scroll = 0
        event.app.invalidate()

    @key_bindings.add("w")
    def _toggle_wrap(event) -> None:
        state.wrap = not state.wrap
        state.detail_scroll = 0
        event.app.invalidate()


def _register_search_keys(key_bindings, state, any_key) -> None:
    """Register search editing keys."""

    @key_bindings.add("/")
    def _start_search(event) -> None:
        state.searching = True
        state.notice = "Search: "
        event.app.invalidate()

    @key_bindings.add("backspace")
    def _search_backspace(event) -> None:
        if not state.searching:
            return
        state.search = state.search[:-1]
        state.selected_index = 0
        state.detail_scroll = 0
        event.app.invalidate()

    @key_bindings.add("enter")
    def _finish_search(event) -> None:
        state.searching = False
        state.notice = ""
        event.app.invalidate()

    @key_bindings.add(any_key)
    def _search_text(event) -> None:
        if not state.searching:
            return
        state.search += event.key_sequence[0].data
        state.selected_index = 0
        state.detail_scroll = 0
        event.app.invalidate()


def _register_copy_and_exit_keys(key_bindings, state, visible_entries) -> None:
    """Register clipboard and exit keys."""

    @key_bindings.add("escape")
    def _escape(event) -> None:
        if state.searching:
            state.searching = False
            state.notice = ""
            event.app.invalidate()
            return
        event.app.exit()

    @key_bindings.add("y")
    def _copy_selected(event) -> None:
        from prompt_toolkit.clipboard import ClipboardData

        entry = selected_entry(state, visible_entries())
        if entry is None:
            return
        event.app.clipboard.set_data(ClipboardData(detail_text(entry, state)))
        state.notice = "Copied selected tool details to clipboard."
        event.app.invalidate()

    @key_bindings.add("c-c")
    @key_bindings.add("q")
    @key_bindings.add("c-o")
    def _quit(event) -> None:
        event.app.exit()


def _other_pane(pane: str) -> Literal["sidebar", "detail"]:
    """Return the opposite inspector pane."""
    return "detail" if pane == "sidebar" else "sidebar"
