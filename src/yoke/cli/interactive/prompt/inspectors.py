"""Fullscreen inspector launchers for the prompt-toolkit CLI."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.process_commands import (
    command_process_manager,
)
from yoke.cli.interactive.process_inspector import (
    open_live_process_inspector,
)
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.tool_inspector import ToolTraceEntry
from yoke.cli.interactive.tool_inspector import ToolTraceStore
from yoke.cli.interactive.tool_inspector import entries_from_messages
from yoke.cli.interactive.tool_inspector import open_live_tool_inspector
from yoke.cli.interactive.tool_inspector import merge_trace_entries
from yoke.cli.runtime import AgentRunner

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession


def create_inspector_launchers(
    *,
    agent: AgentRunner,
    prompt_session: PromptSession,
    state: PromptCliState,
    state_lock: Lock,
    trace_store: ToolTraceStore,
    scrollback: ScrollbackSink,
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Build tool and process inspector callbacks for one prompt session."""
    from prompt_toolkit.application.run_in_terminal import run_in_terminal

    def schedule(open_inspector: Callable[[], None]) -> None:
        app = prompt_session.app
        loop = app.loop
        if loop is None:
            open_inspector()
            return
        loop.call_soon_threadsafe(
            lambda: run_in_terminal(open_inspector, in_executor=True)
        )

    def show_tool_inspector() -> None:
        completed_signature: tuple[int, int | None] | None = None
        completed_entries: list[ToolTraceEntry] = []

        def current_entries() -> list[ToolTraceEntry]:
            nonlocal completed_entries, completed_signature
            with state_lock:
                signature = (id(state.messages), len(state.messages))
                message_snapshot = list(state.messages)
            if signature != completed_signature:
                completed_entries = entries_from_messages(message_snapshot)
                completed_signature = signature
            return merge_trace_entries(completed_entries, trace_store.snapshot())

        def current_revision() -> tuple[int, int, int]:
            with state_lock:
                message_revision = (id(state.messages), len(state.messages))
            return (trace_store.version(), *message_revision)

        schedule(
            lambda: open_live_tool_inspector(
                current_entries,
                trace_store=trace_store,
                revision_provider=current_revision,
            )
        )

    def show_process_inspector() -> None:
        manager = command_process_manager(agent)
        if manager is None:
            scrollback.emit(
                "notice",
                "Process inspection is unavailable for this agent.",
            )
            return
        schedule(lambda: open_live_process_inspector(manager))

    return show_tool_inspector, show_process_inspector
