"""Owned identities for logical turns and physical HTTP workers."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Literal

from yoke.agent.loop import AgentResult
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import ConversationEntry
from yoke.http.runtime.context_usage import RuntimeContextUsageState
from yoke.http.runtime.start import RuntimeAppendPersistence
from yoke.session.admissions import AdmissionRecord
from yoke.http.services.client_session import ClientSession


@dataclass(slots=True)
class ReservedInput:
    """A promoted input and its lease, transferred together into a turn."""

    admission: AdmissionRecord
    workspace_use: ExitStack


@dataclass(slots=True)
class TurnExecution:
    """One promoted input, including resources retained until physical cleanup."""

    turn_id: int
    admission: AdmissionRecord
    started_at: str
    started_monotonic: float
    stop_event: Event
    retired_event: Event
    cold_start: bool
    automatic_title: bool
    client: ClientSession | None = field(default=None, repr=False)
    _tool_count: int = 0
    counter_lock: Lock = field(default_factory=Lock, repr=False)
    append_persistence: RuntimeAppendPersistence | None = None
    context_usage: RuntimeContextUsageState = field(
        default_factory=RuntimeContextUsageState
    )
    task: asyncio.Task[None] | None = None
    slot_acquired: bool = False
    slot_released: bool = False
    worker_started: bool = False
    execution_started: bool = False
    baseline_entry_ids: set[str] = field(default_factory=set)
    workspace_use: ExitStack = field(default_factory=ExitStack)

    def count_tool(self) -> None:
        """Count worker-side starts before loop publication or retirement."""
        with self.counter_lock:
            if not self.retired_event.is_set():
                self._tool_count += 1

    @property
    def tool_count(self) -> int:
        """Read a stable count for turn summaries."""
        with self.counter_lock:
            return self._tool_count


@dataclass(slots=True)
class TurnOutcome:
    """Worker result handed back to the asyncio controller."""

    agent: RuntimeAgent | None
    result: AgentResult | None = None
    error: BaseException | None = None
    partial_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class SessionOperation:
    """One non-prompt runtime operation serialized with session turns."""

    id: str
    kind: Literal["selection", "compaction"]
    started_at: str
    task: asyncio.Task[object] | None = None
