"""Read-only inspector access without exposing runtime managers to the UI."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.models import Message
from yoke.agent.observability import (
    ToolTraceStore,
    ToolTraceEntry,
    ToolTraceOutputChunk,
)
from yoke.agent.observability.tool_transcript import (
    entries_from_messages,
    merge_trace_entries,
)
from yoke.agent.tools.command_process.types import CommandProcessSnapshot


class ProcessInspection:
    """Process queries and notifications. Routes: /process and /process/{id}/output."""

    def __init__(self, manager: CommandProcessManager | None) -> None:
        self._manager = manager

    @property
    def available(self) -> bool:
        return self._manager is not None

    def snapshots(self) -> list[CommandProcessSnapshot]:
        """GET /process?sessionID=, including each retained output tail."""
        return self._manager.snapshots() if self._manager is not None else []

    def subscribe(self, changed: Callable[[], None]) -> Callable[[], None]:
        """GET /event process notifications; polling is also supported."""
        if self._manager is None:
            return lambda: None
        return self._manager.subscribe(changed)

    def agent_runs(self) -> list[dict[str, object]]:
        """GET /agent-run?sessionID={id}."""
        if self._manager is None:
            return []
        return self._manager.agent_runs.snapshots()


class ToolInspection:
    """GET /session/{id}/tool-call, /{call_id}, and /{call_id}/output.

    The in-process adapter merges persisted transcript tools with live events.
    The HTTP implementation will read these same records from the daemon.
    """

    def __init__(
        self, messages: Callable[[], list[Message]], traces: ToolTraceStore
    ) -> None:
        self._messages = messages
        self._traces = traces
        self._signature: tuple[int, int] | None = None
        self._completed: list[ToolTraceEntry] = []

    def snapshots(self) -> list[ToolTraceEntry]:
        messages = self._messages()
        signature = (id(messages), len(messages))
        if signature != self._signature:
            self._completed = entries_from_messages(messages)
            self._signature = signature
        return merge_trace_entries(self._completed, self._traces.snapshot())

    def revision(self) -> tuple[int, int, int]:
        messages = self._messages()
        return self._traces.version(), id(messages), len(messages)

    def call(self, call_id: str) -> ToolTraceEntry | None:
        return next(
            (entry for entry in self.snapshots() if entry.tool_call_id == call_id), None
        )

    def output(self, call_id: str, *, after_seq: int = 0) -> list[ToolTraceOutputChunk]:
        call = self.call(call_id)
        return list(call.output_chunks or [])[after_seq:] if call is not None else []

    def subscribe(self, changed: Callable[[], None]) -> Callable[[], None]:
        return self._traces.subscribe(changed)
