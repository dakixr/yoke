"""Loop-owned turn completion and retirement with executor-backed storage."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import Executor
from time import monotonic
from typing import Literal

from yoke.agent.observability import ToolTraceStore
from yoke.http.runtime.admission import TurnAdmission
from yoke.http.runtime.entry_persistence import with_turn_summary
from yoke.http.runtime.persistence import TurnPersistence, run_blocking
from yoke.http.runtime.resources import SessionRuntimeResources
from yoke.http.runtime.status import RuntimeStatus
from yoke.http.runtime.turns import (
    ReservedInput,
    TurnExecution,
    TurnOutcome,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.session import SessionStore
from yoke.session.workspace import WorkspaceUnavailable

LOGGER = logging.getLogger(__name__)


class TurnFinalizer:
    """Own finalize behavior for one session."""

    def __init__(
        self,
        session_id: str,
        *,
        activity: RuntimeStatus,
        events: EventService,
        gate: asyncio.Lock,
        inputs: TurnAdmission,
        pending_inputs: PendingInputService,
        persistence: TurnPersistence,
        resources: SessionRuntimeResources,
        store: SessionStore,
        tool_traces: ToolTraceStore,
        storage_executor: Executor,
        active_slots: asyncio.Semaphore,
        start: Callable[[ReservedInput], None],
    ) -> None:
        self.session_id = session_id
        self.activity = activity
        self.events = events
        self.gate = gate
        self.inputs = inputs
        self.pending_inputs = pending_inputs
        self.persistence = persistence
        self.resources = resources
        self.store = store
        self.tool_traces = tool_traces
        self.storage_executor = storage_executor
        self.active_slots = active_slots
        self.start = start

    async def finish(
        self,
        execution: TurnExecution,
        outcome: TurnOutcome,
    ) -> None:
        async with self.gate:
            execution.release_slot(self.active_slots)
            if (
                execution.retired_event.is_set()
                or self.activity.active is not execution
            ):
                return
            error = await run_blocking(
                self.storage_executor, self.persist_outcome, execution, outcome
            )
            self.activity.settle_to_idle(error=error)
            next_admission = await self.inputs.reserve_input()
            if next_admission is not None:
                self.start(next_admission)

    async def fail(
        self,
        execution: TurnExecution,
        error: BaseException,
    ) -> None:
        """Recover local controller state after uncertain final persistence."""
        execution.release_slot(self.active_slots)
        LOGGER.error(
            "HTTP runtime finalization failed for session %s input %s.",
            self.session_id,
            execution.admission.id,
            exc_info=(type(error), error, error.__traceback__),
        )
        async with self.gate:
            if self.activity.active is not execution:
                return
            message = "Session finalization failed."
            if isinstance(error, sqlite3.Error):
                message += (
                    f" Could not write {self.store.database_path}: {error}. "
                    "Free disk space or release the database lock, then retry the input."
                )
            self.activity.best_effort_durable(
                "session.runtime.failed",
                {
                    "inputID": execution.admission.id,
                    "turnID": execution.turn_id,
                    "error": message,
                },
            )
            self.activity.settle_to_idle(error=message, hold="finalization_failed")

    async def retire(self, *, reason: Literal["user", "steering", "shutdown"]) -> None:
        execution = self.activity.active
        if execution is None:
            return

        def persist() -> None:
            self.persistence.persist_interruption(
                execution.admission,
                duration_seconds=monotonic() - execution.started_monotonic,
                tool_count=execution.tool_count,
            )
            self.pending_inputs.settle(
                self.session_id,
                execution.admission.id,
                outcome="stopped",
            )
            self.events.durable(
                self.session_id,
                "session.interrupted",
                {
                    "inputID": execution.admission.id,
                    "turnID": execution.turn_id,
                    "reason": reason,
                },
                location=self.activity.event_location,
            )

        try:
            with self.activity.retiring(execution, reason):
                self.tool_traces.retire_turn(execution.turn_id)
                await run_blocking(self.storage_executor, persist)
        finally:
            execution.release_slot(self.active_slots)
            if execution.task is not None and not execution.worker_started:
                execution.task.cancel()

    def persist_outcome(
        self, execution: TurnExecution, outcome: TurnOutcome
    ) -> str | None:
        error: str | None = None
        record = self.persistence.record_for_execution(execution)
        if outcome.result is not None:
            entries = with_turn_summary(
                outcome.result.conversation_entries or [],
                duration_seconds=monotonic() - execution.started_monotonic,
                tool_count=execution.tool_count,
            )
            if entries:
                self.persistence.persist_turn_entries(
                    execution,
                    entries,
                    input_id=execution.admission.id,
                    active_skills=(
                        outcome.agent.active_skills
                        if outcome.agent is not None
                        else record.active_skills
                    ),
                )
            if outcome.result.status == "completed":
                self.resources.promote(outcome.agent)
                settlement = "completed"
            else:
                settlement = "stopped"
            self.pending_inputs.settle(
                self.session_id,
                execution.admission.id,
                outcome=settlement,
            )
            record = self.persistence.record_for_execution(execution)
            self.events.durable(
                self.session_id,
                "session.message.updated",
                {
                    "inputID": execution.admission.id,
                    "turnID": execution.turn_id,
                    "status": outcome.result.status,
                    "leafID": record.leaf_id,
                },
                location=record.root,
            )
        else:
            if outcome.partial_entries:
                partial_entries = with_turn_summary(
                    outcome.partial_entries,
                    duration_seconds=monotonic() - execution.started_monotonic,
                    tool_count=execution.tool_count,
                )
                self.persistence.persist_turn_entries(
                    execution,
                    partial_entries,
                    input_id=execution.admission.id,
                )
                record = self.persistence.record_for_execution(execution)
            if (
                isinstance(outcome.error, WorkspaceUnavailable)
                and not execution.execution_started
            ):
                self.inputs.pause(execution.admission)
            else:
                self.pending_inputs.settle(
                    self.session_id, execution.admission.id, outcome="failed"
                )
            error = self.events.privacy.text(
                str(outcome.error or "Agent execution failed.")
            )
            self.events.durable(
                self.session_id,
                "session.runtime.failed",
                {
                    "inputID": execution.admission.id,
                    "turnID": execution.turn_id,
                    "error": error,
                },
                location=record.root,
            )
        record = execution.context_usage.persist(
            store=self.store,
            events=self.events,
            session_id=self.session_id,
            record=record,
        )
        return error
