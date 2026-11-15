"""Marshal worker events onto the loop that owns logical turn status."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from yoke.agent.activity import activity_status_for_event
from yoke.http.services.redaction import redact_public_value
from yoke.http.services.session_runtime.execution import TurnExecution

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime


class AgentEvents:
    """Keep trace recording on workers and current-generation publication on the loop."""

    def __init__(self, runtime: SessionRuntime) -> None:
        self.activity = runtime.activity
        self.persistence = runtime.persistence
        self.tool_traces = runtime.tool_traces
        self.store = runtime.store
        self.events = runtime.events
        self.session_id = runtime.session_id

    def receive(
        self,
        loop: asyncio.AbstractEventLoop,
        execution: TurnExecution,
        event: str,
        payload: dict[str, object],
    ) -> None:
        if execution.retired_event.is_set():
            return
        traced = dict(payload)
        traced["turn_id"] = execution.turn_id
        self.tool_traces.record_event(event, traced)
        usage_update = execution.context_usage.capture(
            event, payload, turn_id=execution.turn_id, input_id=execution.admission.id
        )
        if event == "model_end" and usage_update is not None:
            with self.persistence.lock:
                if not execution.retired_event.is_set():
                    execution.context_usage.persist_latest(
                        store=self.store, session_id=self.session_id
                    )
        loop.call_soon_threadsafe(
            self.publish, execution, event, dict(payload), usage_update
        )

    def thinking(self, execution: TurnExecution) -> None:
        if execution.retired_event.is_set() or self.activity.active is not execution:
            return
        if self.activity.activity == "Loading session":
            self.activity.activity = "Thinking"
            self.activity.publish_activity(execution)

    def publish(
        self,
        execution: TurnExecution,
        event: str,
        payload: dict[str, object],
        usage_update: dict[str, object] | None,
    ) -> None:
        if execution.retired_event.is_set() or self.activity.active is not execution:
            return
        active_tool_call_ids = self.activity.tool_call_ids.setdefault(
            execution.turn_id,
            set(),
        )
        call_id = payload.get("tool_call_id")
        if event == "tool_execution_start":
            execution.tool_count += 1
            if isinstance(call_id, str):
                active_tool_call_ids.add(call_id)
        elif event == "tool_execution_end" and isinstance(call_id, str):
            active_tool_call_ids.discard(call_id)
        next_activity = activity_status_for_event(
            event,
            payload,
            current=self.activity.activity or "Thinking",
        )
        if event == "tool_execution_end" and active_tool_call_ids:
            next_activity = "Running tool"
        if next_activity is not None and next_activity != self.activity.activity:
            self.activity.activity = next_activity
            self.activity.publish_activity(execution)
        if event == "model_end" and usage_update is not None:
            self.events.live(
                "session.context.updated",
                usage_update,
                session_id=self.session_id,
                location=self.activity.event_location,
            )
        event_type = _PUBLIC_AGENT_EVENTS.get(event)
        if event_type is None:
            return
        if execution.retired_event.is_set() or self.activity.active is not execution:
            return
        if event == "context_usage":
            data = usage_update or {}
        else:
            redacted = redact_public_value(payload)
            data = dict(redacted) if isinstance(redacted, dict) else {"value": redacted}
            data.pop("provider_result_projection", None)
        if event != "context_usage":
            data["turnID"] = execution.turn_id
            data["inputID"] = execution.admission.id
        self.events.live(
            event_type,
            data,
            session_id=self.session_id,
            location=self.activity.event_location,
        )


_PUBLIC_AGENT_EVENTS = {
    "assistant_message": "session.message.updated",
    "tool_execution_start": "session.tool.started",
    "tool_execution_end": "session.tool.ended",
    "compaction_start": "session.compaction.started",
    "compaction_progress": "session.compaction.delta",
    "compaction_end": "session.compaction.ended",
    "context_usage": "session.context.updated",
}
