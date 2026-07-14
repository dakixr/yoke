"""Prompt status update helpers."""

from collections.abc import Callable, Mapping
from threading import Lock

from yoke.cli.interactive.common import PromptCliState


def update_status_context_usage(
    payload: dict[str, object],
    *,
    state: PromptCliState,
    state_lock: Lock,
    invalidate_prompt: Callable[[], None],
    format_context_usage_text: Callable[[Mapping[str, object] | None], str | None],
) -> None:
    """Update prompt-toolkit context usage immediately from an event payload."""
    with state_lock:
        state.context_usage_text = format_context_usage_text(payload)
    invalidate_prompt()
