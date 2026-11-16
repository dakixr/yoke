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
from yoke.cli.interactive.tool_inspector import ToolTraceStore
from yoke.cli.interactive.tool_inspector import open_live_tool_inspector
from yoke.cli.backend.daemon import DaemonSessionBackend


if TYPE_CHECKING:
    from prompt_toolkit import PromptSession


def create_inspector_launchers(
    *,
    agent: DaemonSessionBackend,
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
        inspection = agent.tool_inspection()

        schedule(
            lambda: open_live_tool_inspector(
                inspection.snapshots,
                trace_store=trace_store,
                revision_provider=inspection.revision,
            )
        )

    def show_process_inspector() -> None:
        manager = command_process_manager(agent)
        schedule(lambda: open_live_process_inspector(manager))

    return show_tool_inspector, show_process_inspector
