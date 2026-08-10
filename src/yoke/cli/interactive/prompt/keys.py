"""Prompt-toolkit key bindings for the interactive CLI."""

from __future__ import annotations

from collections.abc import Callable

from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import format_attachment_reference
from yoke.cli.interactive.completion.menu import (
    register_completion_menu_key_bindings,
)
from yoke.cli.interactive.completion.menu import selected_completion
from yoke.cli.interactive.common import PromptCliState

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
) -> None:
    """Register prompt-toolkit key bindings."""
    register_completion_menu_key_bindings(key_bindings)

    @key_bindings.add("escape", "escape")
    def _stop_current_turn(event) -> None:
        if stop_active_turn():
            event.app.invalidate()

    @key_bindings.add("enter")
    def _submit_prompt(event) -> None:
        complete_state = event.current_buffer.complete_state
        completion = selected_completion(complete_state)
        if completion is not None:
            event.current_buffer.apply_completion(completion)
        state.submit_action = "steer"
        event.current_buffer.validate_and_handle()

    @key_bindings.add("tab")
    def _complete_or_queue_prompt(event) -> None:
        complete_state = getattr(event.current_buffer, "complete_state", None)
        completion = selected_completion(complete_state)
        if completion is not None:
            event.current_buffer.apply_completion(completion)
            return
        state.submit_action = "queue"
        event.current_buffer.validate_and_handle()

    @key_bindings.add("escape", "tab")
    @key_bindings.add("s-tab")
    def _cycle_thinking_effort(event) -> None:
        del event
        effort = cycle_thinking_effort()
        state.thinking_effort = effort
        if state.worker is None:
            return
        if effort is None:
            update_status("Thinking effort: default")
            return
        update_status(f"Thinking effort: {effort}")

    @key_bindings.add("c-j")
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    @key_bindings.add("escape", "v")
    @key_bindings.add("c-v")
    def _paste_image_or_text(event) -> None:
        text = event.app.clipboard.get_data().text
        request_clipboard_paste(event.current_buffer, text)

    @key_bindings.add("c-u")
    def _remove_last_image(event) -> None:
        del event
        remove_last_image()

    @key_bindings.add("c-x", "o")
    def _open_tool_inspector(event) -> None:
        del event
        if open_tool_inspector is not None:
            open_tool_inspector()

    @key_bindings.add("c-x", "c-p")
    def _open_process_inspector(event) -> None:
        del event
        if open_process_inspector is not None:
            open_process_inspector()

    @key_bindings.add("c-x", "q")
    def _open_queue_manager(event) -> None:
        if open_queue_manager is None:
            return
        open_queue_manager(event.current_buffer.text)
        event.current_buffer.text = "/queue"
        event.current_buffer.validate_and_handle()

    @key_bindings.add("c-x", "m")
    def _open_model_selector(event) -> None:
        if open_model_selector is None:
            return
        open_model_selector(event.current_buffer.text)
        event.current_buffer.text = "/model"
        event.current_buffer.validate_and_handle()

    @key_bindings.add("c-x", "t")
    def _open_session_tree(event) -> None:
        if open_tree_selector is not None:
            open_tree_selector(event.current_buffer.text)
        event.current_buffer.text = "/tree"
        event.current_buffer.validate_and_handle()

    try:
        key_bindings.add("s-enter")(_insert_newline)
    except ValueError:
        key_bindings.add("escape", "enter")(_insert_newline)


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
