"""Owned identities for logical turns and physical HTTP workers."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Event
from typing import Literal

from yoke.agent.loop import AgentResult
from yoke.agent.models import ConversationEntry
from yoke.http.services.runtime_context_usage import RuntimeContextUsageState
from yoke.http.services.runtime_start import RuntimeAppendPersistence
from yoke.session.admissions import AdmissionRecord


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
    tool_count: int = 0
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


@dataclass(slots=True)
class TurnOutcome:
    """Worker result handed back to the asyncio controller."""

    agent: object | None
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
