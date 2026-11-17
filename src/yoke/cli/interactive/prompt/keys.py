"""Prompt-toolkit key bindings for the interactive CLI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from yoke.cli.image_input import ImageAttachment, format_attachment_reference
from yoke.cli.interactive.common import PromptCliState, session_busy
from yoke.cli.interactive.completion.menu import (
    apply_completion,
    register_completion_menu_key_bindings,
    selected_completion,
)


@dataclass(frozen=True, slots=True)
class Binding:
    """Key sequences and the help shown for one action."""

    sequences: tuple[tuple[str, ...], ...]
    help: str


def _newline_binding() -> Binding:
    from prompt_toolkit.key_binding import KeyBindings

    try:
        KeyBindings().add("s-enter")
    except ValueError:
        return Binding(
            (("c-j",), ("escape", "enter")),
            "Press `Ctrl+J` or `Esc` then `Enter` to insert a newline.",
        )
    return Binding(
        (("c-j",), ("s-enter",)), "Press `Ctrl+J` or `Shift+Enter` to insert a newline."
    )


BINDINGS = {
    "stop": Binding(
        (("escape", "escape"),), "Press `Esc` twice to stop the current turn."
    ),
    "submit": Binding(
        (("enter",),), "Press `Enter` to send, or to steer during a turn."
    ),
    "queue": Binding((("tab",),), "Press `Tab` to queue behind the current turn."),
    "resume": Binding(
        (("c-r",),),
        "Press `Ctrl+R` to resume a held queue. Otherwise it searches history.",
    ),
    "effort": Binding(
        (("escape", "tab"), ("s-tab",)), "Press `Shift+Tab` to cycle thinking effort."
    ),
    "newline": _newline_binding(),
    "paste": Binding(
        (("escape", "v"), ("c-v",)),
        "Press `Ctrl+V` or `Alt+V` to paste text or attach an image.",
    ),
    "remove_image": Binding(
        (("c-u",),), "Press `Ctrl+U` to remove the last pending image."
    ),
    "tools": Binding(
        (("c-x", "o"),), "Press `Ctrl+X` then `O` to open the tool inspector."
    ),
    "processes": Binding(
        (("c-x", "c-p"),), "Press `Ctrl+X` then `Ctrl+P` to open the process inspector."
    ),
    "queue_manager": Binding(
        (("c-x", "q"),), "Press `Ctrl+X` then `Q` to open the queue manager."
    ),
    "model": Binding((("c-x", "m"),), "Press `Ctrl+X` then `M` to switch model."),
    "tree": Binding(
        (("c-x", "t"),), "Press `Ctrl+X` then `T` to open the session tree."
    ),
}
BASIC_BINDINGS = (
    Binding(
        (("enter",),),
        "Press `Enter` to send a line. Inputs sent during a turn are queued.",
    ),
    Binding(
        (("c-c",),),
        "Press `Ctrl+C` to stop and exit, or type `exit` or `quit` to leave.",
    ),
)


def shortcut_notice(*, basic: bool = False) -> str:
    """Build startup and /shortcuts help from the registered bindings."""
    if basic:
        lines = [binding.help for binding in BASIC_BINDINGS]
        lines.append(
            "Use `/image path` to attach an image and `/skill name` to activate a skill."
        )
    else:
        lines = ["Type `exit` or `quit` to leave."]
        lines.extend(binding.help for binding in BINDINGS.values())
        lines.extend(
            (
                "Use `Left` or `Right` to move across prompt text and newlines.",
                "Type `$skill-name` in a prompt to activate one or more skills.",
            )
        )
    lines.append("Use `/shortcuts` or `?` to show this list again.")
    return (
        "Basic mode shortcuts:\n" if basic else "Keyboard shortcuts:\n"
    ) + "\n".join(lines)


DEFAULT_THINKING_EFFORT_VALUES: tuple[str, ...] = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def register_prompt_toolkit_key_bindings(  # noqa: C901
    key_bindings,
    *,
    state: PromptCliState,
    stop_active_turn: Callable[[], bool],
    remove_last_image: Callable[[], None],
    cycle_thinking_effort: Callable[[], str | None],
    update_status: Callable[[str], None],
    request_clipboard_paste: Callable[[object, str], None],
    open_tool_inspector: Callable[[], None] | None = None,
    open_process_inspector: Callable[[], None] | None = None,
    open_model_selector: Callable[[str], None] | None = None,
    open_tree_selector: Callable[[str], None] | None = None,
    open_queue_manager: Callable[[str], None] | None = None,
    resume_queue: Callable[[], None] | None = None,
) -> None:
    """Register prompt-toolkit key bindings."""
    register_completion_menu_key_bindings(key_bindings)

    def bind(name: str, *, when: Callable[[], bool] | None = None):
        def register(handler):
            from prompt_toolkit.filters import Condition

            active = Condition(when) if when is not None else True
            for sequence in BINDINGS[name].sequences:
                key_bindings.add(*sequence, filter=active)(handler)
            return handler

        return register

    def queue_held() -> bool:
        # Only claim Ctrl+R while a queue is held, so it stays history search.
        return (
            resume_queue is not None
            and state.view is not None
            and (state.view.held is not None)
        )

    @bind("stop")
    def _stop_current_turn(event) -> None:
        if stop_active_turn():
            event.app.invalidate()

    @bind("submit")
    def _submit_prompt(event) -> None:
        complete_state = event.current_buffer.complete_state
        completion = selected_completion(complete_state)
        if completion is not None:
            mention = apply_completion(event.current_buffer, completion)
            if mention:
                _insert_mention_separator(event.current_buffer)
                return
        state.submit_action = "steer"
        event.current_buffer.validate_and_handle()

    @bind("queue")
    def _complete_or_queue_prompt(event) -> None:
        complete_state = getattr(event.current_buffer, "complete_state", None)
        completion = selected_completion(complete_state)
        if completion is not None:
            if apply_completion(event.current_buffer, completion):
                _insert_mention_separator(event.current_buffer)
            return
        state.submit_action = "queue"
        event.current_buffer.validate_and_handle()

    @bind("resume", when=queue_held)
    def _resume_queue(event) -> None:
        del event
        if resume_queue is not None:
            resume_queue()

    @bind("effort")
    def _cycle_thinking_effort(event) -> None:
        del event
        effort = cycle_thinking_effort()
        state.thinking_effort = effort
        if not session_busy(state):
            return
        if effort is None:
            update_status("Thinking effort: default")
            return
        update_status(f"Thinking effort: {effort}")

    @bind("newline")
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    @bind("paste")
    def _paste_image_or_text(event) -> None:
        text = event.app.clipboard.get_data().text
        request_clipboard_paste(event.current_buffer, text)

    @bind("remove_image")
    def _remove_last_image(event) -> None:
        del event
        remove_last_image()

    @bind("tools")
    def _open_tool_inspector(event) -> None:
        del event
        if open_tool_inspector is not None:
            open_tool_inspector()

    @bind("processes")
    def _open_process_inspector(event) -> None:
        del event
        if open_process_inspector is not None:
            open_process_inspector()

    @bind("queue_manager")
    def _open_queue_manager(event) -> None:
        if open_queue_manager is None:
            return
        open_queue_manager(event.current_buffer.text)
        event.current_buffer.text = "/queue"
        event.current_buffer.validate_and_handle()

    @bind("model")
    def _open_model_selector(event) -> None:
        if open_model_selector is None:
            return
        open_model_selector(event.current_buffer.text)
        event.current_buffer.text = "/model"
        event.current_buffer.validate_and_handle()

    @bind("tree")
    def _open_session_tree(event) -> None:
        if open_tree_selector is not None:
            open_tree_selector(event.current_buffer.text)
        event.current_buffer.text = "/tree"
        event.current_buffer.validate_and_handle()


def _insert_mention_separator(buffer: Any) -> None:
    after = getattr(getattr(buffer, "document", None), "text_after_cursor", "")
    if not after or (not after[0].isspace() and after[0] not in ".,;:!?)]}"):
        buffer.insert_text(" ")


def cycle_prompt_thinking_effort(
    current: str | None,
    values: tuple[str, ...] = DEFAULT_THINKING_EFFORT_VALUES,
) -> str:
    """Return the next configured thinking effort value."""
    normalized_values = tuple(
        value.strip().lower() for value in values if value.strip()
    )
    if not normalized_values:
        normalized_values = DEFAULT_THINKING_EFFORT_VALUES
    current_value = current.strip().lower() if current else "high"
    try:
        index = normalized_values.index(current_value)
    except ValueError:
        index = normalized_values.index("high") if "high" in normalized_values else -1
    return normalized_values[(index + 1) % len(normalized_values)]


def insert_attachment_reference(buffer, attachment: ImageAttachment) -> None:
    """Insert an image reference token at the current cursor position."""
    reference = format_attachment_reference(attachment)
    before = buffer.document.char_before_cursor
    after = buffer.document.current_char
    prefix = "" if before is None or before.isspace() else " "
    suffix = "" if after is None or after.isspace() else " "
    if after is None:
        suffix = ""
    buffer.insert_text(f"{prefix}{reference}{suffix}")
