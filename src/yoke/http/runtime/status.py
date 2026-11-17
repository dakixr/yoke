"""The loop-owned runtime phase, transition validation and publication."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from yoke.http.errors import session_busy
from yoke.http.models.session import ActiveRuntimeInfo, RuntimeOperationInfo
from yoke.http.runtime.phase import (
    Activity,
    Idle,
    OperationActive,
    Retiring,
    RuntimePhase,
    RuntimeState,
    TurnActive,
    next_phase,
    phase_name,
    wire_state,
)
from yoke.http.runtime.turns import SessionOperation, TurnExecution
from yoke.http.services.event_broker import EventService

LOGGER = logging.getLogger(__name__)


class RuntimeStatus:
    """Own the logical lane separately from workers retained for physical cleanup."""

    def __init__(
        self, session_id: str, events: EventService, event_location: str | None
    ) -> None:
        self.session_id = session_id
        self.events = events
        self.event_location = event_location
        self._phase: RuntimePhase = Idle()
        self._revision = 0
        self.tool_call_ids: dict[int, set[str]] = {}
        self.changed = asyncio.Event()

    @property
    def phase(self) -> RuntimePhase:
        return self._phase

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def state(self) -> RuntimeState:
        return wire_state(self.phase)

    @property
    def last_error(self) -> str | None:
        return self.phase.last_error if isinstance(self.phase, Idle) else None

    @property
    def active(self) -> TurnExecution | None:
        return (
            self.phase.execution
            if isinstance(self.phase, (TurnActive, Retiring))
            else None
        )

    @active.setter
    def active(self, execution: TurnExecution | None) -> None:
        # Retained in-process integrations used this to install a synthetic turn.
        # It must obey the same transition rules as ordinary admission.
        self.enter(
            TurnActive(execution, Activity.THINKING) if execution else Idle(),
            cause="active_assignment",
        )

    @property
    def operation(self) -> SessionOperation | None:
        return self.phase.operation if isinstance(self.phase, OperationActive) else None

    @property
    def activity(self) -> str | None:
        match self.phase:
            case TurnActive(activity=activity):
                return activity.value
            case OperationActive(operation=operation):
                return (
                    "Compacting"
                    if operation.kind == "compaction"
                    else "Selecting model"
                )
            case _:
                return None

    def enter(self, new: RuntimePhase, *, cause: str) -> None:
        """Validate, install, log and publish one authoritative transition."""
        previous = self.phase
        new = next_phase(previous, new)
        if isinstance(new, Retiring):
            new.execution.stop_event.set()
            new.execution.retired_event.set()
        self._phase = new
        self._revision += 1
        if (
            isinstance(previous, (TurnActive, Retiring))
            and self.active is not previous.execution
        ):
            self.tool_call_ids.pop(previous.execution.turn_id, None)
        self.check_invariants()
        LOGGER.debug(
            "machine=runtime id=%s from=%s to=%s cause=%s revision=%s",
            self.session_id,
            phase_name(previous),
            phase_name(new),
            cause,
            self.revision,
        )
        self.changed.set()
        self.best_effort_publish_activity(self.active)

    def check_invariants(self) -> None:
        """Raise during state-check tests and report invariant failures in production."""
        valid = (self.active is None and self.operation is None) == isinstance(
            self.phase, Idle
        )
        if isinstance(self.phase, Retiring):
            valid = valid and self.phase.execution.retired_event.is_set()
        if not valid:
            message = f"Runtime ownership invariant failed for {self.session_id}: {self.phase!r}"
            if os.environ.get("YOKE_STATE_CHECKS") == "1":
                raise AssertionError(message)
            LOGGER.error(message)

    def require_idle(self) -> None:
        if not isinstance(self.phase, Idle):
            raise session_busy()

    def set_activity(self, execution: TurnExecution, activity: Activity) -> None:
        """Ignore late worker callbacks after retirement or handoff."""
        if (
            isinstance(self.phase, TurnActive)
            and self.active is execution
            and not execution.retired_event.is_set()
        ):
            if self.phase.activity != activity:
                self.enter(TurnActive(execution, activity), cause="activity")

    @contextmanager
    def retiring(
        self, execution: TurnExecution, reason: Literal["user", "steering", "shutdown"]
    ) -> Iterator[None]:
        """Fence first and guarantee a published idle phase even if storage fails."""
        self.enter(Retiring(execution, reason), cause=reason)
        try:
            yield
        except BaseException as error:
            self.best_effort_durable(
                "session.runtime.failed",
                {
                    "inputID": execution.admission.id,
                    "turnID": execution.turn_id,
                    "error": str(error) or type(error).__name__,
                },
            )
            self.settle_to_idle(
                error=str(error) or type(error).__name__, hold="finalization_failed"
            )
            raise
        else:
            self.settle_to_idle(hold=reason)

    @contextmanager
    def operating(self, operation: SessionOperation) -> Iterator[None]:
        """Clear operation ownership on every failure, including cancellation."""
        try:
            yield
        except BaseException as error:
            if self.operation is operation:
                message = str(error) or type(error).__name__
                self.best_effort_durable(
                    f"session.{operation.kind}.failed",
                    {
                        "operationID": operation.id,
                        "error": message,
                    },
                )
                self.settle_to_idle(error=message, hold=f"{operation.kind}_failed")
            raise

    def track_operation(
        self, operation: SessionOperation, task: asyncio.Task[object]
    ) -> None:
        """Also settle a task cancelled before its coroutine first runs."""
        operation.task = task

        def finished(done: asyncio.Task[object]) -> None:
            if done.cancelled() and self.operation is operation:
                try:
                    with self.operating(operation):
                        raise asyncio.CancelledError
                except asyncio.CancelledError:
                    pass

        task.add_done_callback(finished)

    def settle_to_idle(
        self, *, error: str | None = None, hold: str | None = None
    ) -> None:
        """Publish an exit and record why queued work must remain stopped."""
        self.enter(Idle(error, hold), cause=hold or "settled")

    def snapshot(self) -> ActiveRuntimeInfo:
        """Use the same projection for snapshots and live state events."""
        active, operation = self.active, self.operation
        return ActiveRuntimeInfo(
            state=self.state,
            phase=phase_name(self.phase),
            revision=self.revision,
            turn_id=active.turn_id if active else None,
            input_id=active.admission.id if active else None,
            started_at=active.started_at
            if active
            else operation.started_at
            if operation
            else None,
            operation=RuntimeOperationInfo(id=operation.id, kind=operation.kind)
            if operation
            else None,
            activity=self.activity,
            error=self.last_error,
            last_error=self.last_error,
        )

    def publish_activity(self, execution: TurnExecution | None) -> None:
        self.events.live(
            "session.active.changed",
            self.snapshot().model_dump(
                mode="json", by_alias=True, exclude={"last_error"}
            ),
            session_id=self.session_id,
            location=self.event_location,
        )

    def best_effort_durable(self, event_type: str, data: dict[str, object]) -> None:
        try:
            self.events.durable(
                self.session_id, event_type, data, location=self.event_location
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "Failed to journal %s for session %s.", event_type, self.session_id
            )

    def best_effort_publish_activity(self, execution: TurnExecution | None) -> None:
        try:
            self.publish_activity(execution)
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "Failed to publish terminal activity for session %s.", self.session_id
            )
