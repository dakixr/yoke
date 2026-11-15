"""Idle model selection and compaction operations for one session."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from yoke.agent.compaction import force_compact_agent
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.provider_selection import (
    ProviderSessionState,
    switch_agent_provider_model,
)
from yoke.http.services.runtime_factory import http_session_usage_metric_context
from yoke.http.services.session_runtime.execution import (
    SessionOperation,
)
from yoke.http.services.session_runtime.workspace import (
    require_runtime_workspace,
    uses_workspace,
)
from yoke.session import SessionRecord

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime

LOGGER = logging.getLogger(__name__)


class SessionOperations:
    """Own operations behavior for one session."""

    def __init__(self, runtime: SessionRuntime) -> None:
        self.runtime = runtime
        self.active_slots = runtime.active_slots
        self.activity = runtime.activity
        self.events = runtime.events
        self.executor = runtime.executor
        self.gate = runtime.gate
        self.inputs = runtime.inputs
        self.persistence = runtime.persistence
        self.resources = runtime.resources
        self.session_id = runtime.session_id
        self.start = runtime.start
        self.store = runtime.store

    async def select_model(
        self,
        *,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        """Apply one provider/model selection while the session is idle."""
        async with self.gate:
            self.activity.require_idle()
            await self.runtime.run_blocking(require_runtime_workspace, self.runtime)
            operation = self._new_operation("selection")
            self.activity.operation = operation
            task = asyncio.create_task(
                self._run_selection(
                    operation,
                    provider_name=provider_name,
                    model_id=model_id,
                    reasoning_effort=reasoning_effort,
                ),
                name=f"yoke-http-selection-{self.session_id}",
            )
            operation.task = task
        result = await task
        assert isinstance(result, ProviderSessionState)
        return result

    async def compact(self) -> str:
        """Schedule one manual compaction and return its operation identity."""
        async with self.gate:
            self.activity.require_idle()
            await self.runtime.run_blocking(require_runtime_workspace, self.runtime)
            operation = self._new_operation("compaction")
            self.activity.operation = operation
            self.activity.state = "running"
            self.activity.last_error = None
            self.activity.activity = "Compacting"
            self.events.durable(
                self.session_id,
                "session.compaction.started",
                {"operationID": operation.id, "reason": "manual"},
                location=self.activity.event_location,
            )
            self.activity.publish_activity(None)
            task = asyncio.create_task(
                self._run_compaction(operation),
                name=f"yoke-http-compaction-{self.session_id}",
            )
            operation.task = task
            return operation.id

    async def _run_selection(
        self,
        operation: SessionOperation,
        *,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        await self.active_slots.acquire()
        try:
            loop = asyncio.get_running_loop()
            state = await loop.run_in_executor(
                self.executor,
                self._select_sync,
                provider_name,
                model_id,
                reasoning_effort,
            )
        except BaseException as exc:
            await self._finish_operation_error(operation, exc)
            raise
        finally:
            self.active_slots.release()
        await self._finish_operation_success(operation)
        return state

    @uses_workspace
    def _select_sync(
        self,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        record, agent = self._primary()
        try:
            with http_session_usage_metric_context(record):
                state = switch_agent_provider_model(
                    agent,
                    provider_name=provider_name,
                    model_id=model_id,
                    reasoning_effort=reasoning_effort,
                    session_id=self.session_id,
                    retire_previous=self.resources.retire_provider,
                )
        finally:
            self.resources.register_provider(agent.provider)
        updated = self.store.set_selection(
            self.session_id,
            provider_name=state.provider_name,
            model_id=state.model_id,
            reasoning_effort=state.reasoning_effort,
            context_window_tokens=state.context_window_tokens,
            existing_record=record,
        )
        self.events.durable(
            self.session_id,
            "session.selection.changed",
            {
                "provider": updated.provider_name,
                "model": updated.model_id,
                "reasoningEffort": updated.reasoning_effort,
            },
            location=updated.root,
        )
        return state

    async def _run_compaction(self, operation: SessionOperation) -> object:
        await self.active_slots.acquire()
        try:
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(self.executor, self._compact_sync)
        except Exception as exc:  # accepted operations settle into runtime status
            await self._finish_operation_error(operation, exc)
            return {"compacted": False}
        finally:
            self.active_slots.release()
        await self._finish_operation_success(operation)
        return payload

    @uses_workspace
    def _compact_sync(self) -> dict[str, object]:
        record, agent = self._primary()
        with http_session_usage_metric_context(record):
            compacted = force_compact_agent(
                agent,
                agent.messages,
                conversation_entries=agent.conversation_entries,
            )
        payload: dict[str, object]
        if compacted is None:
            payload = {"compacted": False}
        else:
            self.persistence.persist_entries(
                compacted.conversation_entries,
                input_id=None,
                active_skills=agent.active_skills,
            )
            payload = {
                "compacted": True,
                "summarizedMessages": len(compacted.preparation.messages_to_summarize),
                "keptMessages": len(compacted.preparation.kept_messages),
                "messageCount": len(compacted.messages),
                "inputTokens": compacted.preparation.estimate.input_tokens,
                "compactedInputTokens": compacted.compacted_estimate.input_tokens,
            }
        self.events.durable(
            self.session_id,
            "session.compaction.ended",
            payload,
            location=self.activity.event_location,
        )
        return payload

    async def _finish_operation_success(self, operation: SessionOperation) -> None:
        async with self.gate:
            if self.activity.operation is not operation:
                return
            self.activity.operation = None
            self.activity.last_error = None
            self.activity.activity = None
            next_admission = await self.runtime.reserve_input()
            if next_admission is not None:
                self.start(next_admission)
            else:
                self.activity.state = "error" if self.activity.last_error else "idle"
                self.activity.publish_activity(None)

    async def _finish_operation_error(
        self,
        operation: SessionOperation,
        error: BaseException,
    ) -> None:
        async with self.gate:
            if self.activity.operation is not operation:
                return
            self.activity.operation = None
            self.activity.state = "error"
            self.activity.last_error = str(error)
            self.activity.activity = None
            self.activity.best_effort_durable(
                f"session.{operation.kind}.failed",
                {"operationID": operation.id, "error": self.activity.last_error},
            )
            self.activity.best_effort_publish_activity(None)
            if operation.kind == "compaction":
                next_admission = await self.runtime.reserve_input()
                if next_admission is not None:
                    self.start(next_admission)

    def _new_operation(
        self,
        kind: Literal["selection", "compaction"],
    ) -> SessionOperation:
        return SessionOperation(
            id=f"op_{secrets.token_hex(12)}",
            kind=kind,
            started_at=datetime.now(UTC).isoformat(),
        )

    def _primary(self) -> tuple[SessionRecord, RuntimeAgent]:
        snapshot = self.persistence.snapshot()
        agent = self.resources.ensure_primary(
            snapshot.record,
            load_state=True,
            active_entries=snapshot.owned_active_path(),
        )
        if not isinstance(agent, RuntimeAgent):
            raise ValueError("This operation requires a RuntimeAgent-backed session.")
        return snapshot.record, agent
