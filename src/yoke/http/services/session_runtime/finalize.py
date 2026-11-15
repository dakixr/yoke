"""Loop-owned turn completion and retirement with executor-backed storage."""

from __future__ import annotations

import logging
from time import monotonic
from typing import TYPE_CHECKING

from yoke.http.services.runtime_persistence import with_turn_summary
from yoke.http.services.session_runtime.execution import (
    TurnExecution,
    TurnOutcome,
)
from yoke.session.workspace import WorkspaceUnavailable

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime

LOGGER = logging.getLogger(__name__)


class TurnFinalizer:
    """Own finalize behavior for one session."""

    def __init__(self, runtime: SessionRuntime) -> None:
        self.runtime = runtime
        self.activity = runtime.activity
        self.events = runtime.events
        self.gate = runtime.gate
        self.inputs = runtime.inputs
        self.pending_inputs = runtime.pending_inputs
        self.persistence = runtime.persistence
        self.release_slot = runtime.release_slot
        self.resources = runtime.resources
        self.session_id = runtime.session_id
        self.start = runtime.start
        self.store = runtime.store
        self.tool_traces = runtime.tool_traces

    async def finish(
        self,
        execution: TurnExecution,
        outcome: TurnOutcome,
    ) -> None:
        async with self.gate:
            self.release_slot(execution)
            if (
                execution.retired_event.is_set()
                or self.activity.active is not execution
            ):
                return
            self.activity.last_error = await self.runtime.run_blocking(
                self.persist_outcome, execution, outcome
            )
            self.activity.active = None
            self.activity.activity = None
            self.activity.tool_call_ids.pop(execution.turn_id, None)
            next_admission = await self.runtime.reserve_input()
            if next_admission is not None:
                self.start(next_admission)
            else:
                self.activity.state = "error" if self.activity.last_error else "idle"
                self.activity.publish_activity(None)

    async def fail(
        self,
        execution: TurnExecution,
        error: BaseException,
    ) -> None:
        """Recover local controller state after uncertain final persistence."""
        self.release_slot(execution)
        LOGGER.error(
            "HTTP runtime finalization failed for session %s input %s.",
            self.session_id,
            execution.admission.id,
            exc_info=(type(error), error, error.__traceback__),
        )
        async with self.gate:
            if self.activity.active is not execution:
                return
            self.activity.active = None
            self.activity.state = "error"
            self.activity.last_error = "Session finalization failed."
            self.activity.activity = None
            self.activity.tool_call_ids.pop(execution.turn_id, None)
            self.activity.best_effort_durable(
                "session.runtime.failed",
                {
                    "inputID": execution.admission.id,
                    "turnID": execution.turn_id,
                    "error": self.activity.last_error,
                },
            )
            self.activity.best_effort_publish_activity(None)

    async def retire(self, *, reason: str) -> None:
        execution = self.activity.active
        if execution is None:
            return
        self.activity.state = "stopping"
        execution.stop_event.set()
        execution.retired_event.set()
        self.tool_traces.retire_turn(execution.turn_id)

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

        await self.runtime.run_blocking(persist)
        self.release_slot(execution)
        if execution.task is not None and not execution.worker_started:
            execution.task.cancel()
        self.activity.active = None
        self.activity.state = "idle"
        self.activity.activity = None
        self.activity.tool_call_ids.pop(execution.turn_id, None)
        self.activity.publish_activity(None)

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
                self.runtime.inputs.pause(execution.admission)
            else:
                self.pending_inputs.settle(
                    self.session_id, execution.admission.id, outcome="failed"
                )
            error = str(outcome.error or "Agent execution failed.")
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
