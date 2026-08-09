"""Fullscreen prompt-toolkit inspector for tool call traces."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from typing import Literal
from yoke.cli.interactive.tool_inspector.trace import ToolTraceEntry
from yoke.cli.interactive.tool_inspector.trace import ToolTraceContext
from yoke.cli.interactive.tool_inspector.trace import ToolTraceStore
from yoke.cli.interactive.tool_inspector.mouse import handle_mouse_event
from yoke.cli.interactive.tool_inspector.render import detail_text
from yoke.cli.interactive.tool_inspector.render import entry_text
from yoke.cli.interactive.tool_inspector.render import move_selection
from yoke.cli.interactive.tool_inspector.render import page_step
from yoke.cli.interactive.tool_inspector.render import render_view_html
from yoke.cli.interactive.tool_inspector.render import selected_entry
from yoke.cli.interactive.tool_inspector.render import sidebar_items
from yoke.cli.interactive.tool_inspector.render import ToolInspectorItem
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)


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
    data_revision: int = 0
    source_revision: object | None = None
    detail_cache_key: tuple[object, ...] | None = field(default=None, repr=False)
    detail_cache_lines: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        """Start on the newest sidebar item by default."""
        items = sidebar_items(self.entries)
        if items:
            self.selected_index = len(items) - 1


def open_tool_inspector(entries: Sequence[ToolTraceEntry]) -> None:
    """Open a fullscreen alternate-buffer view of tool calls."""
    open_live_tool_inspector(lambda: list(entries), revision_provider=lambda: 0)


def open_live_tool_inspector(
    entries_provider: Callable[[], Sequence[ToolTraceEntry]],
    *,
    trace_store: ToolTraceStore | None = None,
    revision_provider: Callable[[], object] | None = None,
) -> None:
    """Open a fullscreen alternate-buffer view of live tool calls."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseEvent

    if revision_provider is None and trace_store is not None:
        revision_provider = trace_store.version
    initial_revision = revision_provider() if revision_provider is not None else None
    state = ToolInspectorState(
        entries=list(entries_provider()),
        source_revision=initial_revision,
    )
    key_bindings = KeyBindings()
    visible_cache_key: tuple[int, str] | None = None
    visible_cache: list[ToolInspectorItem] = []
    search_index_revision = -1
    search_index: list[tuple[ToolInspectorItem, str]] = []

    def visible_entries() -> list[ToolInspectorItem]:
        nonlocal search_index, search_index_revision
        nonlocal visible_cache_key, visible_cache
        _refresh_entries(state, entries_provider, revision_provider)
        query = state.search.strip().lower()
        cache_key = (state.data_revision, query)
        if cache_key == visible_cache_key:
            return visible_cache
        items = sidebar_items(state.entries)
        if not query:
            visible_cache = items
        else:
            if search_index_revision != state.data_revision:
                search_index = [(item, entry_text(item)) for item in items]
                search_index_revision = state.data_revision
            visible_cache = [item for item, text in search_index if query in text]
        visible_cache_key = cache_key
        return visible_cache

    def formatted_rows() -> HTML:
        visible = visible_entries()
        return HTML(render_view_html(state, visible))

    def handle_mouse(mouse_event: MouseEvent) -> None:
        visible = visible_entries()
        handle_mouse_event(state, visible, mouse_event)
        app.invalidate()
        return None

    _register_tool_inspector_keys(
        key_bindings,
        state=state,
        visible_entries=visible_entries,
        any_key=Keys.Any,
    )

    class ToolInspectorControl(FormattedTextControl):
        def mouse_handler(self, mouse_event: MouseEvent) -> None:
            return handle_mouse(mouse_event)

    control = ToolInspectorControl(formatted_rows, focusable=True)
    app: Application[None] = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=True,
        min_redraw_interval=0.05,
    )
    unsubscribe = None
    if trace_store is not None:
        unsubscribe = trace_store.subscribe(lambda: app.invalidate())
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            try:
                app.run()
            finally:
                if unsubscribe is not None:
                    unsubscribe()


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


def _register_navigation_keys(key_bindings, state, visible_entries) -> None:  # noqa: C901
    """Register selection and scroll keys."""
    from prompt_toolkit.keys import Keys

    @key_bindings.add(Keys.Down)
    @key_bindings.add("j")
    def _move_down(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, visible_entries(), 1)
        else:
            state.detail_scroll += 1
        event.app.invalidate()

    @key_bindings.add(Keys.Up)
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

    @key_bindings.add(Keys.Left)
    def _focus_sidebar(event) -> None:
        state.active_pane = "sidebar"
        event.app.invalidate()

    @key_bindings.add(Keys.Right)
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
    from prompt_toolkit.filters import Condition

    is_searching = Condition(lambda: state.searching)

    @key_bindings.add("/")
    def _start_search(event) -> None:
        state.searching = True
        state.notice = "Search: "
        event.app.invalidate()

    @key_bindings.add("backspace", filter=is_searching)
    def _search_backspace(event) -> None:
        state.search = state.search[:-1]
        state.selected_index = 0
        state.detail_scroll = 0
        event.app.invalidate()

    @key_bindings.add("enter", filter=is_searching)
    def _finish_search(event) -> None:
        state.searching = False
        state.notice = ""
        event.app.invalidate()

    @key_bindings.add(any_key, filter=is_searching)
    def _search_text(event) -> None:
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


def _refresh_entries(
    state: ToolInspectorState,
    entries_provider: Callable[[], Sequence[ToolTraceEntry]],
    revision_provider: Callable[[], object] | None = None,
) -> None:
    """Refresh state entries while preserving selection when possible."""
    next_revision = revision_provider() if revision_provider is not None else None
    if next_revision is not None and next_revision == state.source_revision:
        return
    current_items = sidebar_items(state.entries)
    current_key = _item_key(selected_entry(state, current_items))
    was_at_tail = bool(current_items) and state.selected_index >= len(current_items) - 1
    next_entries = list(entries_provider())
    state.source_revision = next_revision
    if next_revision is None and next_entries == state.entries:
        return
    state.entries = next_entries
    state.data_revision += 1
    state.detail_cache_key = None
    state.detail_cache_lines.clear()
    next_items = sidebar_items(state.entries)
    if not next_items:
        state.selected_index = 0
        return
    if was_at_tail:
        state.selected_index = len(next_items) - 1
        return
    if current_key is None:
        state.selected_index = min(state.selected_index, len(next_items) - 1)
        return
    for index, item in enumerate(next_items):
        if _item_key(item) == current_key:
            state.selected_index = index
            return
    state.selected_index = min(state.selected_index, len(next_items) - 1)


def _item_key(item: ToolInspectorItem | None) -> tuple[str, str] | None:
    if item is None:
        return None
    if isinstance(item, ToolTraceContext):
        return ("context", f"{item.role}:{item.text}")
    return ("tool", item.tool_call_id)
