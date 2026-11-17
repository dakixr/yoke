"""Owned identities for logical turns and physical HTTP workers."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import IntEnum
import logging
import os
from threading import Event, Lock
from typing import Literal

from yoke.agent.loop import AgentResult
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import ConversationEntry
from yoke.http.runtime.context_usage import RuntimeContextUsageState
from yoke.http.runtime.phase import IllegalTransition
from yoke.http.runtime.start import RuntimeAppendPersistence
from yoke.session.admissions import AdmissionRecord
from yoke.http.services.client_session import ClientSession


LOGGER = logging.getLogger(__name__)


class ExecPhase(IntEnum):
    """Physical execution milestones, independent of logical retirement."""

    RESERVED = 0
    WAITING_SLOT = 1
    SLOT_HELD = 2
    WORKER_RUNNING = 3
    EXECUTING = 4
    DONE = 5


def next_exec_phase(current: ExecPhase, new: ExecPhase) -> ExecPhase:
    """Validate a physical milestone or terminal cleanup."""
    if (current != ExecPhase.DONE and new == ExecPhase.DONE) or (
        current != ExecPhase.DONE and new.value == current.value + 1
    ):
        return new
    raise IllegalTransition(f"{current.name} -> {new.name}")


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
    _phase: ExecPhase = field(default=ExecPhase.RESERVED, init=False)
    _done_from: ExecPhase = field(default=ExecPhase.RESERVED, init=False)
    _phase_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    baseline_entry_ids: set[str] = field(default_factory=set)
    workspace_use: ExitStack = field(default_factory=ExitStack)

    @property
    def phase(self) -> ExecPhase:
        with self._phase_lock:
            return self._phase

    def _enter(self, new: ExecPhase) -> None:
        previous = self._phase
        self._phase = next_exec_phase(previous, new)
        if new == ExecPhase.DONE:
            self._done_from = previous
        self._check_invariants()
        LOGGER.debug(
            "machine=execution id=%s from=%s to=%s cause=milestone",
            self.turn_id,
            previous.name,
            new.name,
        )

    def check_invariants(self) -> None:
        """Validate terminal provenance while the execution is stable."""
        with self._phase_lock:
            self._check_invariants()

    def _check_invariants(self) -> None:
        valid = isinstance(self._phase, ExecPhase) and (
            self._phase != ExecPhase.DONE or self._done_from != ExecPhase.DONE
        )
        if not valid:
            message = f"Execution ownership invariant failed for turn {self.turn_id}"
            if os.environ.get("YOKE_STATE_CHECKS") == "1":
                raise AssertionError(message)
            LOGGER.error(message)

    def complete_without_slot(self) -> None:
        """Finish an admission cancelled before it acquired capacity."""
        with self._phase_lock:
            if self._phase < ExecPhase.SLOT_HELD:
                self._enter(ExecPhase.DONE)

    def wait_for_slot(self) -> None:
        with self._phase_lock:
            self._enter(ExecPhase.WAITING_SLOT)

    def acquired_slot(self) -> None:
        with self._phase_lock:
            self._enter(ExecPhase.SLOT_HELD)

    def start_worker(self) -> None:
        with self._phase_lock:
            self._enter(ExecPhase.WORKER_RUNNING)

    def start_execution(self) -> None:
        with self._phase_lock:
            # execute() also supports callers that manage their own worker and
            # never enter the HTTP capacity lane.
            if self._phase not in {ExecPhase.RESERVED, ExecPhase.DONE}:
                self._enter(ExecPhase.EXECUTING)

    @property
    def worker_started(self) -> bool:
        with self._phase_lock:
            milestone = (
                self._done_from if self._phase == ExecPhase.DONE else self._phase
            )
            return milestone >= ExecPhase.WORKER_RUNNING

    @property
    def execution_started(self) -> bool:
        with self._phase_lock:
            milestone = (
                self._done_from if self._phase == ExecPhase.DONE else self._phase
            )
            return milestone >= ExecPhase.EXECUTING

    def release_slot(self, slots: asyncio.Semaphore) -> None:
        """Release acquired capacity once, including retirement/cleanup races."""
        with self._phase_lock:
            if self._phase < ExecPhase.SLOT_HELD or self._phase == ExecPhase.DONE:
                return
            self._enter(ExecPhase.DONE)
            slots.release()

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
