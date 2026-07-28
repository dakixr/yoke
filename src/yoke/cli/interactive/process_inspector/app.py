"""Fullscreen prompt-toolkit inspector for live command processes."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from yoke.agent.tools import CommandProcessManager
from yoke.agent.tools import CommandProcessSnapshot
from yoke.cli.interactive.process_inspector.render import detail_text
from yoke.cli.interactive.process_inspector.render import move_selection
from yoke.cli.interactive.process_inspector.render import page_step
from yoke.cli.interactive.process_inspector.render import render_view_html
from yoke.cli.interactive.process_inspector.render import selected_process
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)


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

    def __post_init__(self) -> None:
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

    state = ProcessInspectorState(manager.snapshots())
    bindings = KeyBindings()

    def current_processes() -> list[CommandProcessSnapshot]:
        _refresh_processes(state, manager.snapshots())
        return state.processes

    control = FormattedTextControl(
        lambda: HTML(render_view_html(state, current_processes())), focusable=True
    )
    app: Application[None] = Application(
        layout=Layout(Window(content=control, always_hide_cursor=True)),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=False,
        refresh_interval=0.5,
    )
    _register_keys(bindings, state, current_processes, Keys.Up, Keys.Down)
    unsubscribe = manager.subscribe(app.invalidate)
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            try:
                app.run()
            finally:
                unsubscribe()


def _register_keys(bindings, state, current_processes, up, down) -> None:  # noqa: C901
    @bindings.add(down)
    @bindings.add("j")
    def _down(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, current_processes(), 1)
        else:
            state.detail_scroll += 1
        event.app.invalidate()

    @bindings.add(up)
    @bindings.add("k")
    def _up(event) -> None:
        if state.active_pane == "sidebar":
            move_selection(state, current_processes(), -1)
        else:
            state.detail_scroll = max(0, state.detail_scroll - 1)
        event.app.invalidate()

    @bindings.add("left")
    def _left(event) -> None:
        state.active_pane = "sidebar"
        event.app.invalidate()

    @bindings.add("right")
    def _right(event) -> None:
        state.active_pane = "detail"
        event.app.invalidate()

    @bindings.add("h")
    @bindings.add("l")
    def _toggle_pane(event) -> None:
        state.active_pane = "detail" if state.active_pane == "sidebar" else "sidebar"
        event.app.invalidate()

    @bindings.add("pageup")
    def _page_up(event) -> None:
        state.detail_scroll = max(0, state.detail_scroll - page_step())
        event.app.invalidate()

    @bindings.add("pagedown")
    def _page_down(event) -> None:
        state.detail_scroll += page_step()
        event.app.invalidate()

    @bindings.add("home")
    @bindings.add("g")
    def _home(event) -> None:
        if state.active_pane == "sidebar":
            state.selected_index = 0
        state.detail_scroll = 0
        event.app.invalidate()

    @bindings.add("end")
    @bindings.add("G")
    def _end(event) -> None:
        if state.active_pane == "sidebar":
            state.selected_index = max(0, len(current_processes()) - 1)
            state.detail_scroll = 0
        else:
            state.detail_scroll = 10**9
        event.app.invalidate()

    @bindings.add("w")
    def _wrap(event) -> None:
        state.wrap = not state.wrap
        state.detail_scroll = 0
        event.app.invalidate()

    @bindings.add("y")
    def _copy(event) -> None:
        from prompt_toolkit.clipboard import ClipboardData

        process = selected_process(state, current_processes())
        if process is not None:
            event.app.clipboard.set_data(ClipboardData(detail_text(process)))
            state.notice = "Copied selected process details to clipboard."
            event.app.invalidate()

    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("q")
    def _quit(event) -> None:
        event.app.exit()


def _refresh_processes(
    state: ProcessInspectorState, processes: list[CommandProcessSnapshot]
) -> None:
    selected = selected_process(state, state.processes)
    selected_id = selected.session_id if selected is not None else None
    was_at_tail = (
        bool(state.processes) and state.selected_index == len(state.processes) - 1
    )
    state.processes = processes
    if not processes:
        state.selected_index = 0
    elif was_at_tail:
        state.selected_index = len(processes) - 1
    else:
        state.selected_index = next(
            (
                index
                for index, process in enumerate(processes)
                if process.session_id == selected_id
            ),
            min(state.selected_index, len(processes) - 1),
        )
