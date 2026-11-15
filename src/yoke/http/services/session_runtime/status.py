"""Loop-owned logical runtime status and activity publication."""

from __future__ import annotations

import logging
from typing import Literal

from yoke.http.errors import session_busy
from yoke.http.services.event_broker import EventService
from yoke.http.services.session_runtime.execution import (
    SessionOperation,
    TurnExecution,
)

type RuntimeState = Literal["idle", "running", "stopping", "waiting_input", "error"]
LOGGER = logging.getLogger(__name__)


class RuntimeStatus:
    """Loop-owned status and publication for the current logical generation."""

    def __init__(
        self, session_id: str, events: EventService, event_location: str | None
    ) -> None:
        self.session_id = session_id
        self.events = events
        self.event_location = event_location
        self.state: RuntimeState = "idle"
        self.last_error: str | None = None
        self.activity: str | None = None
        self.active: TurnExecution | None = None
        self.operation: SessionOperation | None = None
        self.tool_call_ids: dict[int, set[str]] = {}

    def require_idle(self) -> None:
        if self.active is not None or self.operation is not None:
            raise session_busy()

    def publish_activity(self, execution: TurnExecution | None) -> None:
        if execution is not None and (
            execution.retired_event.is_set() or self.active is not execution
        ):
            return
        self.events.live(
            "session.active.changed",
            {
                "state": self.state,
                "turnID": execution.turn_id if execution is not None else None,
                "startedAt": execution.started_at if execution is not None else None,
                "error": self.last_error,
                "activity": self.activity,
            },
            session_id=self.session_id,
            location=self.event_location,
        )

    def best_effort_durable(
        self,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        """Report a controller failure without making cleanup depend on the journal."""
        try:
            self.events.durable(
                self.session_id,
                event_type,
                data,
                location=self.event_location,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "Failed to journal %s for session %s.",
                event_type,
                self.session_id,
            )

    def best_effort_publish_activity(
        self,
        execution: TurnExecution | None,
    ) -> None:
        """Publish terminal local state without making cleanup depend on the broker."""
        try:
            self.publish_activity(execution)
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "Failed to publish terminal activity for session %s.",
                self.session_id,
            )
