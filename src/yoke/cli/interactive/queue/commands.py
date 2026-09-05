"""Slash-command adapter for the prompt queue manager."""

from __future__ import annotations

from collections.abc import Callable

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.queue.manager import edit_queue_prompt
from yoke.cli.interactive.queue.manager import open_queue_manager
from yoke.cli.render.base import Console


def handle_queue_command(
    normalized: str,
    *,
    console: Console,
    pending_prompts: list[PendingPrompt] | None,
    on_queue_changed: Callable[[], None] | None,
    on_queue_replace: Callable[[list[PendingPrompt]], str | None] | None,
) -> bool:
    """Handle a queue command and report whether it matched."""
    if normalized != "/queue" and not normalized.startswith("/queue "):
        return False
    from yoke.cli.render import print_scrollback_notice

    if pending_prompts is None:
        print_scrollback_notice(
            console,
            "/queue is only available in the prompt-toolkit TUI.",
        )
        return True
    if normalized != "/queue":
        print_scrollback_notice(console, "Use /queue without arguments.")
        return True
    updated = open_queue_manager(
        pending_prompts,
        edit_prompt=edit_queue_prompt,
    )
    if updated is None:
        print_scrollback_notice(console, "Queue manager cancelled.")
        return True
    if on_queue_replace is not None:
        conflict_notice = on_queue_replace(updated)
        if conflict_notice is not None:
            print_scrollback_notice(console, conflict_notice)
            return True
    else:
        pending_prompts[:] = updated
        if on_queue_changed is not None:
            on_queue_changed()
    print_scrollback_notice(console, f"Queue updated: {len(updated)} pending.")
    return True
