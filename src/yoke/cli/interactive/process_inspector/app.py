"""Fullscreen prompt-toolkit inspector for live command processes."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
import threading
import time
from typing import Literal

from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.cli.interactive.process_inspector.render import detail_text
from yoke.cli.interactive.process_inspector.render import DetailOutputCache
from yoke.cli.interactive.process_inspector.render import move_selection
from yoke.cli.interactive.process_inspector.render import page_step
from yoke.cli.interactive.process_inspector.render import (
    render_view_html,
)
from yoke.cli.interactive.process_inspector.render import (
    selected_process,
)
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)

PROCESS_INSPECTOR_MIN_REDRAW_INTERVAL = 0.1
PROCESS_SNAPSHOT_REFRESH_INTERVAL = 0.5


class ProcessSnapshotCache:
    """Coalesce process notifications and cache decoded snapshots."""

    def __init__(
        self,
        manager: CommandProcessManager,
        *,
        refresh_interval: float = PROCESS_SNAPSHOT_REFRESH_INTERVAL,
    ) -> None:
        self._manager = manager
        self._refresh_interval = refresh_interval
        self._dirty = threading.Event()
        self._processes = manager.snapshots()
        self._refreshed_at = time.monotonic()

    def mark_dirty(self) -> None:
        """Record that the manager has newer process state."""
        self._dirty.set()

    def snapshots(self) -> list[CommandProcessSnapshot]:
        """Refresh once per change batch or periodic elapsed-time tick."""
        now = time.monotonic()
        if (
            not self._dirty.is_set()
            and now - self._refreshed_at < self._refresh_interval
        ):
            return self._processes
        # Clear before reading so a notification concurrent with snapshots()
        # remains set for the next render instead of being lost.
        self._dirty.clear()
        self._processes = self._manager.snapshots()
        self._refreshed_at = now
        return self._processes


@dataclass(slots=True)
class ProcessInspectorState:
    """Mutable UI state for the process inspector."""

    processes: list[CommandProcessSnapshot]
    selected_index: int = 0
    list_scroll: int = 0
    detail_scroll: int = 0
    wrap: bool = True
    notice: str = ""
    active_pane: Literal["sidebar", "detail"] = "sidebar"
    detail_output_cache: DetailOutputCache = field(default_factory=DetailOutputCache)

    def __post_init__(self) -> None:
        """Start with the newest retained process selected."""
        if self.processes:
            self.selected_index = len(self.processes) - 1


def open_live_process_inspector(manager: CommandProcessManager) -> None:
    """Open a fullscreen alternate-buffer view of command processes."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    snapshot_cache = ProcessSnapshotCache(manager)
    state = ProcessInspectorState(snapshot_cache.snapshots())
    key_bindings = KeyBindings()

    def current_processes() -> list[CommandProcessSnapshot]:
        _refresh_processes(state, snapshot_cache.snapshots())
        return state.processes

    def formatted_rows() -> HTML:
        return HTML(render_view_html(state, current_processes()))

    _register_keys(
        key_bindings,
        state=state,
        current_processes=current_processes,
        up=Keys.Up,
        down=Keys.Down,
        left=Keys.Left,
        right=Keys.Right,
    )
    control = FormattedTextControl(formatted_rows, focusable=True)
    app: Application[None] = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=False,
        min_redraw_interval=PROCESS_INSPECTOR_MIN_REDRAW_INTERVAL,
        refresh_interval=PROCESS_SNAPSHOT_REFRESH_INTERVAL,
    )

    def process_changed() -> None:
        snapshot_cache.mark_dirty()
        app.invalidate()

    unsubscribe = manager.subscribe(process_changed)
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            try:
                app.run()
            finally:
                unsubscribe()


def _register_keys(  # noqa: C901
    key_bindings,
    *,
    state: ProcessInspectorState,
    current_processes,
    up,
    down,
    left,
    right,
) -> None:
    @key_bindings.add(down)
    @key_bindings.add("j")
    def _move_down(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, current_processes(), 1)
        else:
            state.detail_scroll += 1
        event.app.invalidate()

    @key_bindings.add(up)
    @key_bindings.add("k")
    def _move_up(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, current_processes(), -1)
        else:
            state.detail_scroll = max(0, state.detail_scroll - 1)
        event.app.invalidate()

    @key_bindings.add(left)
    def _focus_sidebar(event) -> None:
        state.active_pane = "sidebar"
        event.app.invalidate()

    @key_bindings.add(right)
    def _focus_detail(event) -> None:
        state.active_pane = "detail"
        event.app.invalidate()

    @key_bindings.add("h")
    @key_bindings.add("l")
    def _toggle_pane(event) -> None:
        state.active_pane = "detail" if state.active_pane == "sidebar" else "sidebar"
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        state.detail_scroll = max(0, state.detail_scroll - page_step())
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        state.detail_scroll += page_step()
        event.app.invalidate()

    @key_bindings.add("home")
    @key_bindings.add("g")
    def _home(event) -> None:
        if state.active_pane == "sidebar":
            state.selected_index = 0
        state.detail_scroll = 0
        event.app.invalidate()

    @key_bindings.add("end")
    @key_bindings.add("G")
    def _end(event) -> None:
        if state.active_pane == "sidebar":
            state.selected_index = max(0, len(current_processes()) - 1)
            state.detail_scroll = 0
        else:
            state.detail_scroll = 10**9
        event.app.invalidate()

    @key_bindings.add("w")
    def _toggle_wrap(event) -> None:
        state.wrap = not state.wrap
        state.detail_scroll = 0
        event.app.invalidate()

    @key_bindings.add("y")
    def _copy_selected(event) -> None:
        from prompt_toolkit.clipboard import ClipboardData

        process = selected_process(state, current_processes())
        if process is None:
            return
        event.app.clipboard.set_data(ClipboardData(detail_text(process)))
        state.notice = "Copied selected process details to clipboard."
        event.app.invalidate()

    @key_bindings.add("escape")
    @key_bindings.add("c-c")
    @key_bindings.add("q")
    def _quit(event) -> None:
        event.app.exit()


def _refresh_processes(
    state: ProcessInspectorState,
    processes: list[CommandProcessSnapshot],
) -> None:
    """Refresh snapshots while preserving the selected session ID."""
    selected = selected_process(state, state.processes)
    selected_id = selected.session_id if selected is not None else None
    was_at_tail = bool(state.processes) and state.selected_index >= (
        len(state.processes) - 1
    )
    state.processes = processes
    if not processes:
        state.selected_index = 0
        return
    if was_at_tail:
        state.selected_index = len(processes) - 1
        return
    for index, process in enumerate(processes):
        if process.session_id == selected_id:
            state.selected_index = index
            return
    state.selected_index = min(state.selected_index, len(processes) - 1)
