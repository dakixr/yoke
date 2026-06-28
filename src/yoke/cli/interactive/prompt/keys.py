"""Prompt-toolkit key bindings for the interactive CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import format_attachment_reference
from yoke.cli.image_input import paste_image_from_clipboard
from yoke.cli.interactive.completion_menu import (
    register_completion_menu_key_bindings,
)
from yoke.cli.interactive.completion_menu import selected_completion
from yoke.cli.interactive.common import PromptCliState

THINKING_EFFORT_VALUES: tuple[str, ...] = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
)


def register_prompt_toolkit_key_bindings(  # noqa: C901
    key_bindings,
    *,
    state: PromptCliState,
    stop_active_turn: Callable[[], bool],
    attach_image: Callable[[ImageAttachment], None],
    remove_last_image: Callable[[], None],
    resolve_image_path: Callable[[str], Path],
    cycle_thinking_effort: Callable[[], str | None],
    update_status: Callable[[str], None],
    open_tool_inspector: Callable[[], None] | None = None,
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

    @key_bindings.add("c-v")
    def _paste_image_or_text(event) -> None:
        attachment = paste_image_from_clipboard()
        if attachment is not None:
            attach_image(attachment)
            insert_attachment_reference(
                event.current_buffer,
                attachment,
            )
            return
        text = event.app.clipboard.get_data().text
        if not text:
            return
        try:
            attachment = ImageAttachment(path=resolve_image_path(text))
            attach_image(attachment)
            insert_attachment_reference(
                event.current_buffer,
                attachment,
            )
        except ValueError:
            event.current_buffer.insert_text(text)

    @key_bindings.add("c-u")
    def _remove_last_image(event) -> None:
        del event
        remove_last_image()

    @key_bindings.add("c-o")
    def _open_tool_inspector(event) -> None:
        del event
        if open_tool_inspector is not None:
            open_tool_inspector()

    @key_bindings.add("c-q")
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
    def _open_tree_selector(event) -> None:
        if open_tree_selector is None:
            return
        open_tree_selector(event.current_buffer.text)
        event.current_buffer.text = "/tree"
        event.current_buffer.validate_and_handle()

    try:
        key_bindings.add("s-enter")(_insert_newline)
    except ValueError:
        key_bindings.add("escape", "enter")(_insert_newline)


def cycle_prompt_thinking_effort(
    current: str | None,
    available_efforts: tuple[str, ...] | None = None,
) -> str | None:
    """Return the next configured thinking effort value."""
    values = THINKING_EFFORT_VALUES if available_efforts is None else available_efforts
    if not values:
        return None
    normalized_current = current.strip().lower() if current else None
    default_index = max(len(values) - 2, 0)
    try:
        index = values.index(normalized_current or values[default_index])
    except ValueError:
        index = default_index
    return values[(index + 1) % len(values)]


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
