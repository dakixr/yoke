"""Fullscreen prompt queue manager for prompt-toolkit mode."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)


@dataclass(slots=True)
class QueueManagerState:
    """Mutable queue manager UI state."""

    prompts: list[PendingPrompt]
    selected_index: int = 0
    notice: str = ""


@dataclass(frozen=True, slots=True)
class _QueueManagerEditRequest:
    """Request to edit an item after the fullscreen app exits."""

    index: int


def open_queue_manager(
    prompts: Sequence[PendingPrompt],
    *,
    edit_prompt: Callable[[PendingPrompt], PendingPrompt | None],
) -> list[PendingPrompt] | None:
    """Open a fullscreen queue manager and return updated prompts."""
    state = QueueManagerState(prompts=[prompt.copy_for_queue() for prompt in prompts])
    changed = False
    while True:
        result = _run_queue_manager(state, prompts=prompts, changed=changed)
        if isinstance(result, _QueueManagerEditRequest):
            if result.index >= len(state.prompts):
                state.notice = "Queue item no longer exists."
                continue
            edited = edit_prompt(state.prompts[result.index])
            if edited is None:
                state.notice = "Edit cancelled."
                continue
            state.prompts[result.index] = edited
            state.selected_index = result.index
            state.notice = "Saved queue item."
            changed = True
            continue
        return result


def _run_queue_manager(
    state: QueueManagerState,
    *,
    prompts: Sequence[PendingPrompt],
    changed: bool,
) -> list[PendingPrompt] | _QueueManagerEditRequest | None:
    """Run one queue manager application instance."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    key_bindings = KeyBindings()

    def selected_prompt() -> PendingPrompt | None:
        if not state.prompts:
            return None
        state.selected_index = min(state.selected_index, len(state.prompts) - 1)
        return state.prompts[state.selected_index]

    def mutate(message: str) -> None:
        nonlocal changed
        changed = True
        state.notice = message

    def formatted_rows() -> HTML:
        return HTML(render_queue_manager_html(state))

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event) -> None:
        if state.prompts:
            state.selected_index = min(len(state.prompts) - 1, state.selected_index + 1)
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event) -> None:
        state.selected_index = max(0, state.selected_index - 1)
        event.app.invalidate()

    @key_bindings.add("enter")
    @key_bindings.add("e")
    def _edit(event) -> None:
        selected = selected_prompt()
        if selected is None:
            state.notice = "Queue is empty."
            event.app.invalidate()
            return
        event.app.exit(result=_QueueManagerEditRequest(state.selected_index))

    @key_bindings.add("d")
    @key_bindings.add("delete")
    def _delete(event) -> None:
        selected = selected_prompt()
        if selected is None:
            return
        state.prompts.pop(state.selected_index)
        state.selected_index = max(0, min(state.selected_index, len(state.prompts) - 1))
        mutate("Deleted queue item.")
        event.app.invalidate()

    @key_bindings.add("p")
    def _promote(event) -> None:
        selected = selected_prompt()
        if selected is None:
            return
        state.prompts.pop(state.selected_index)
        state.prompts.insert(0, selected)
        state.selected_index = 0
        mutate("Promoted item to next.")
        event.app.invalidate()

    @key_bindings.add("s")
    def _toggle_steering(event) -> None:
        selected = selected_prompt()
        if selected is None:
            return
        selected.kind = "queued" if selected.kind == "steering" else "steering"
        if selected.kind == "steering":
            state.prompts.pop(state.selected_index)
            state.prompts.insert(_first_non_steering_index(state.prompts), selected)
            state.selected_index = state.prompts.index(selected)
        mutate(f"Marked item as {selected.kind}.")
        event.app.invalidate()

    @key_bindings.add("space")
    def _toggle_paused(event) -> None:
        selected = selected_prompt()
        if selected is None:
            return
        selected.paused = not selected.paused
        mutate("Paused item." if selected.paused else "Resumed item.")
        event.app.invalidate()

    @key_bindings.add("c-k")
    @key_bindings.add("escape", "up")
    def _move_item_up(event) -> None:
        if state.selected_index <= 0:
            return
        index = state.selected_index
        state.prompts[index - 1], state.prompts[index] = (
            state.prompts[index],
            state.prompts[index - 1],
        )
        state.selected_index -= 1
        mutate("Moved item up.")
        event.app.invalidate()

    @key_bindings.add("c-j")
    @key_bindings.add("escape", "down")
    def _move_item_down(event) -> None:
        if state.selected_index >= len(state.prompts) - 1:
            return
        index = state.selected_index
        state.prompts[index + 1], state.prompts[index] = (
            state.prompts[index],
            state.prompts[index + 1],
        )
        state.selected_index += 1
        mutate("Moved item down.")
        event.app.invalidate()

    @key_bindings.add("escape")
    @key_bindings.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    @key_bindings.add("q")
    @key_bindings.add("c-q")
    def _save_and_close(event) -> None:
        event.app.exit(result=state.prompts if changed else list(prompts))

    control = FormattedTextControl(formatted_rows, focusable=True)
    app: Application[list[PendingPrompt] | _QueueManagerEditRequest | None] = (
        Application(
            layout=Layout(Window(content=control, always_hide_cursor=True)),
            key_bindings=key_bindings,
            full_screen=True,
            mouse_support=False,
        )
    )
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            return app.run()
    return None


def edit_queue_prompt(prompt: PendingPrompt) -> PendingPrompt | None:
    """Edit one queue item using prompt-toolkit's multiline prompt."""
    from prompt_toolkit import PromptSession

    key_bindings = queue_edit_key_bindings()
    session: PromptSession[str] = PromptSession(multiline=True)
    with suppress(EOFError, KeyboardInterrupt):
        edited = session.prompt(
            "edit queued prompt › ",
            default=prompt.prompt,
            key_bindings=key_bindings,
            bottom_toolbar="Enter saves · Ctrl+J newline · Ctrl+C cancels",
        )
        if not edited.strip():
            return None
        updated = prompt.copy_for_queue()
        updated.prompt = edited
        if updated.user_message is not None:
            updated.user_message = None
        return updated
    return None


def queue_edit_key_bindings():
    """Return key bindings for the queue item editor."""
    from prompt_toolkit.key_binding import KeyBindings

    key_bindings = KeyBindings()

    @key_bindings.add("enter")
    def _save(event) -> None:
        event.current_buffer.validate_and_handle()

    @key_bindings.add("c-j")
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    return key_bindings


def _first_non_steering_index(prompts: list[PendingPrompt]) -> int:
    """Return where a newly steering prompt should be inserted."""
    for index, prompt in enumerate(prompts):
        if prompt.kind != "steering" or prompt.paused:
            return index
    return len(prompts)


def render_queue_manager_html(state: QueueManagerState) -> str:
    """Render the queue manager as prompt-toolkit HTML."""
    lines = [
        "<b>Prompt Queue</b>",
        "<ansidim>↑/↓ select · Enter/e edit · d delete · p promote · s steering · Space pause · Alt+↑/↓ move · q save</ansidim>",
        "",
    ]
    if not state.prompts:
        lines.append("<ansidim>No queued prompts.</ansidim>")
    for index, prompt in enumerate(state.prompts, start=1):
        selected = index - 1 == state.selected_index
        marker = "▶" if selected else " "
        state_label = "paused" if prompt.paused else prompt.kind
        preview = _escape(_preview(prompt.prompt, 88))
        line = f"{marker} {index}. [{state_label}] {preview}"
        if selected:
            line = f"<reverse>{line}</reverse>"
        lines.append(line)
    selected = state.prompts[state.selected_index] if state.prompts else None
    lines.append("")
    lines.append("<b>Preview</b>")
    if selected is None:
        lines.append("<ansidim>Nothing selected.</ansidim>")
    else:
        for line in _wrap(selected.prompt, 100)[:18]:
            lines.append(_escape(line))
    if state.notice:
        lines.append("")
        lines.append(f"<ansiyellow>{_escape(state.notice)}</ansiyellow>")
    return "\n".join(lines)


def _preview(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _wrap(text: str, width: int) -> list[str]:
    rows: list[str] = []
    for line in text.splitlines() or [""]:
        rows.extend(textwrap.wrap(line, width=width) or [""])
    return rows


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
