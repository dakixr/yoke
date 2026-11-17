"""The asyncio admission and lifecycle gate for one HTTP-owned session."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor
from datetime import UTC, datetime
from functools import partial
from threading import Event
from time import monotonic

from yoke.agent.observability import ToolTraceStore
from yoke.agent.tools.command_process import CommandProcessManager
from yoke.http.models.session import ActiveRuntimeInfo, InterruptData
from yoke.http.reads.cache import SessionReadCache
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.runtime.admission import TurnAdmission
from yoke.http.runtime.execution import TurnExecutor
from yoke.http.runtime.factory import SessionAgentFactory
from yoke.http.runtime.finalization import TurnFinalizer
from yoke.http.runtime.operations import SessionOperations
from yoke.http.runtime.persistence import TurnPersistence, run_blocking
from yoke.http.runtime.resources import SessionRuntimeResources
from yoke.http.runtime.status import RuntimeStatus
from yoke.http.runtime.phase import Activity, Idle, TurnActive
from yoke.http.runtime.title import SessionTitleAutomation
from yoke.http.runtime.turns import (
    ReservedInput,
    TurnExecution,
)
from yoke.http.runtime.workers import WorkerTracker
from yoke.http.runtime.workspace import (
    require_runtime_workspace,
)
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.mcp.config import McpSessionPolicy
from yoke.session import SessionRecord, SessionStore
from yoke.session.admissions import AdmissionRecord
from yoke.session.workspace import WorkspaceBusy, WorkspaceUnavailable

LOGGER = logging.getLogger(__name__)


class SessionRuntime:
    """Serialize admission and lifecycle through one asyncio gate."""

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        pending_inputs: PendingInputService,
        events: EventService,
        agent_factory: SessionAgentFactory,
        read_cache: SessionReadCache,
        message_index: SessionMessageIndex | None,
        indexed_runtime_seed: bool,
        executor: Executor,
        storage_executor: Executor,
        active_slots: asyncio.Semaphore,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.pending_inputs = pending_inputs
        self.events = events
        self.agent_factory = agent_factory
        self.read_cache = read_cache
        self.message_index = message_index
        self.indexed_runtime_seed = indexed_runtime_seed
        index_entry = store.index_entry(session_id)
        self.executor = executor
        self.storage_executor = storage_executor
        self.active_slots = active_slots
        self.gate = asyncio.Lock()
        self.resources = SessionRuntimeResources(
            session_id=session_id,
            agent_factory=agent_factory,
            executor=executor,
            on_process_change=self.on_process_change,
            store=store,
            privacy=events.privacy,
        )
        self.tool_traces = ToolTraceStore()
        self.workers = WorkerTracker()
        self.activity = RuntimeStatus(
            session_id, events, index_entry.root if index_entry is not None else None
        )
        self.persistence = TurnPersistence(
            session_id, store, pending_inputs, read_cache
        )
        self.automatic_title = SessionTitleAutomation(
            session_id,
            store=store,
            events=events,
            read_cache=read_cache,
            agent_factory=self.resources.build_agent,
            executor=executor,
            persistence_lock=self.persistence.lock,
            user_message_factory=self.persistence.user_message,
        )
        self._turn_counter = 0
        self.inputs = TurnAdmission(
            session_id,
            store=store,
            pending_inputs=pending_inputs,
            persistence=self.persistence,
            events=events,
            activity=self.activity,
            storage_executor=storage_executor,
        )
        self.finalizer = TurnFinalizer(
            session_id,
            activity=self.activity,
            events=events,
            gate=self.gate,
            inputs=self.inputs,
            pending_inputs=pending_inputs,
            persistence=self.persistence,
            resources=self.resources,
            store=store,
            tool_traces=self.tool_traces,
            storage_executor=storage_executor,
            active_slots=active_slots,
            start=self.start,
        )
        self.execution = TurnExecutor(
            session_id,
            store=store,
            events=events,
            activity=self.activity,
            resources=self.resources,
            persistence=self.persistence,
            automatic_title=self.automatic_title,
            tool_traces=self.tool_traces,
            workers=self.workers,
            executor=executor,
            active_slots=active_slots,
            finalizer=self.finalizer,
            message_index=message_index,
            indexed_runtime_seed=indexed_runtime_seed,
            activate_queued_skill=lambda name: self.operations.activate_queued_skill(
                name
            ),
        )
        self.operations = SessionOperations(
            session_id,
            store=store,
            events=events,
            activity=self.activity,
            resources=self.resources,
            persistence=self.persistence,
            inputs=self.inputs,
            gate=self.gate,
            active_slots=active_slots,
            executor=executor,
            storage_executor=storage_executor,
            start=self.start,
            automatic_title=self.automatic_title,
            workers=self.workers,
            message_index=message_index,
        )
        self.activity.hold_queue = lambda reason: pending_inputs.set_hold(
            session_id, reason
        )
        self.reserve_input = self.inputs.reserve_input
        self.idle_mutation = self.operations.idle_mutation
        self.run_blocking = partial(run_blocking, storage_executor)
        self.select_model = self.operations.select_model
        self.compact = self.operations.compact
        self.activate_skill = self.operations.activate_skill
        self.set_tools = self.operations.set_tools
        self.set_mcp_policy = self.operations.set_mcp_policy

    async def wake(self) -> None:
        """Start eligible work or apply a pending steer at a safe control boundary."""
        async with self.gate:
            await self.run_blocking(self.pending_inputs.set_hold, self.session_id, None)
            try:
                await self.run_blocking(require_runtime_workspace, self)
            except WorkspaceUnavailable as exc:
                if self.activity.active is None and self.activity.operation is None:
                    self.activity.enter(
                        Idle(str(exc), "workspace_unavailable"),
                        cause="workspace_unavailable",
                    )
                return
            await self.run_blocking(self._reopen_archived_locked)
            if self.activity.operation is not None:
                return
            if self.activity.active is not None:
                steering = await self.reserve_input(allow_queue=False, recover=False)
                if steering is None:
                    return
                with steering.workspace_use:
                    await self.finalizer.retire(reason="steering")
                    self.start(steering)
                return
            admission = await self.reserve_input()
            if admission is not None:
                self.start(admission)

    def _reopen_archived_locked(self) -> None:
        record = self.store.summary_record(self.session_id)
        if record is None or record.archived_at is None:
            return
        record = self.store.set_archived(
            self.session_id,
            False,
            existing_record=record,
        )
        self.events.durable(
            self.session_id,
            "session.updated",
            {
                "sessionID": record.id,
                "title": record.title,
                "pinned": record.pinned,
                "archivedAt": record.archived_at,
            },
            location=record.root,
        )

    async def interrupt(self) -> tuple[bool, int | None]:
        """Retire the current generation without consuming queued work."""
        result = await self.interrupt_result()
        return result.interrupted, result.turn_id

    async def interrupt_result(self) -> InterruptData:
        """Report why an operation cannot be interrupted under the same gate."""
        async with self.gate:
            if self.activity.operation is not None:
                return InterruptData(
                    interrupted=False, reason="operation_not_interruptible"
                )
            if self.activity.active is None:
                return InterruptData(interrupted=False)
            turn_id = self.activity.active.turn_id
            await self.finalizer.retire(reason="user")
            return InterruptData(interrupted=True, turn_id=turn_id)

    async def hold_if_idle(self) -> None:
        """An explicit no-resume request must leave visible, quiescent work."""
        async with self.gate:
            if self.activity.active is not None or self.activity.operation is not None:
                return
            queue = await self.run_blocking(self.pending_inputs.queue, self.session_id)
            if queue.held is None and any(not item.paused for item in queue.items):
                await self.run_blocking(
                    self.pending_inputs.set_hold, self.session_id, "manual"
                )

    async def cancel_input(self, input_id: str) -> None:
        """Cancel only the named input under the runtime's admission gate."""
        from yoke.http.errors import ApiError
        from yoke.http.models.prompt import QueuePatchRequest

        async with self.gate:
            pending = self.pending_inputs
            record = await self.run_blocking(
                pending.get_input, self.session_id, input_id
            )
            if record.phase.kind in {"settled", "removed"}:
                raise ApiError(
                    409, "input_settled", "Input already reached a terminal phase."
                )
            if record.phase.kind in {"queued", "paused"}:
                queue = await self.run_blocking(pending.queue, self.session_id)
                await self.run_blocking(
                    pending.patch_queue,
                    self.session_id,
                    QueuePatchRequest.model_validate(
                        {
                            "expectedRevision": queue.revision,
                            "operations": [{"op": "remove", "id": input_id}],
                            "resume": False,
                        }
                    ),
                )
            elif (
                self.activity.active is not None
                and self.activity.active.admission.id == input_id
            ):
                await self.finalizer.retire(reason="user")
            elif self.activity.active is None:
                await self.run_blocking(
                    pending.settle, self.session_id, input_id, outcome="stopped"
                )
                await self.run_blocking(pending.set_hold, self.session_id, "interrupt")
            else:
                raise ApiError(
                    409, "input_not_active", "Another input owns the active turn."
                )

    async def wait(self, timeout_seconds: float | None = None) -> ActiveRuntimeInfo:
        """Wait until the current logical generation settles."""

        async def wait_idle() -> None:
            while True:
                async with self.gate:
                    if self.activity.active is None and self.activity.operation is None:
                        return
                    task = (
                        self.activity.active.task
                        if self.activity.active is not None
                        else self.activity.operation.task
                        if self.activity.operation is not None
                        else None
                    )
                    self.activity.changed.clear()
                if task is None:
                    await self.activity.changed.wait()
                else:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if not task.cancelled():
                            raise

        if timeout_seconds is None:
            await wait_idle()
        else:
            await asyncio.wait_for(wait_idle(), timeout_seconds)
        return await self.status()

    async def status(self) -> ActiveRuntimeInfo:
        """Return process-local activity without loading durable session metadata."""
        async with self.gate:
            return self.activity.snapshot()

    async def close(self) -> None:
        """Stop current work and release the primary runtime."""
        operation_task: asyncio.Task[object] | None = None
        try:
            async with self.gate:
                if self.activity.active is not None:
                    await self.finalizer.retire(reason="shutdown")
                operation_task = (
                    self.activity.operation.task
                    if self.activity.operation is not None
                    else None
                )
            if operation_task is not None:
                try:
                    await asyncio.shield(operation_task)
                except Exception:  # noqa: BLE001
                    LOGGER.exception(
                        "Session operation failed during runtime shutdown: %s",
                        self.session_id,
                    )
            await self.automatic_title.close()
        finally:
            await self.resources.close()

    def agent_run_registry(self):
        """Expose one observation scope across all clients' process managers."""
        self.resources.agent_runs.refresh()
        return self.resources.agent_runs

    def process_managers(self) -> list[CommandProcessManager]:
        """Return every client manager through the runtime's public seam."""
        return self.resources.process_managers()

    def process_manager(self) -> CommandProcessManager | None:
        """Return the live command manager when this runtime has built an agent."""
        return self.resources.process_manager()

    def latest_turn_id(self) -> int:
        """Return the newest process-local turn id without touching persistence."""
        return self._turn_counter

    def cancel_automatic_title(self) -> None:
        """Prevent an in-flight automatic title from replacing an explicit edit."""
        self.automatic_title.cancel()

    def session_enabled_tool_names(self) -> set[str] | None:
        """Return the process-local tool allowlist without loading an agent."""
        self.resources.synchronize_workspace()
        return self.resources.session_enabled_tool_names()

    def mcp_session_policy(self) -> McpSessionPolicy:
        """Return a defensive process-local MCP policy snapshot."""
        self.resources.synchronize_workspace()
        return self.resources.mcp_session_policy()

    def start(self, reserved: ReservedInput) -> None:
        with reserved.workspace_use:
            try:
                execution = self.launch_execution(reserved.admission)
                execution.workspace_use = reserved.workspace_use.pop_all()
            except BaseException as exc:
                self.activity.enter(
                    Idle(str(exc), "start_failed"), cause="start_failed"
                )
                raise

    def launch_execution(self, admission: AdmissionRecord) -> TurnExecution:
        self._turn_counter += 1
        cold_start = (
            not self.resources.has_primary()
            and not self.read_cache.is_current(self.session_id)
        )
        summary = self.store.summary_record(self.session_id)
        automatic_title = bool(
            summary is not None and not summary.title and summary.leaf_id is None
        )
        execution = TurnExecution(
            turn_id=self._turn_counter,
            admission=admission,
            started_at=datetime.now(UTC).isoformat(),
            started_monotonic=monotonic(),
            stop_event=Event(),
            retired_event=Event(),
            cold_start=cold_start,
            automatic_title=automatic_title,
            client=self.resources.configuration.clients.for_input(admission.id),
        )
        self.activity.tool_call_ids = {execution.turn_id: set()}
        self.activity.enter(
            TurnActive(
                execution,
                Activity.WAITING_SLOT
                if self.active_slots.locked()
                else Activity.LOADING
                if cold_start
                else Activity.THINKING,
            ),
            cause="start",
        )
        self.execution.launch(execution)
        return execution

    def on_process_change(self) -> None:
        self.events.live(
            "session.process.updated",
            {},
            session_id=self.session_id,
            location=self.activity.event_location,
        )

    async def relocate_workspace(
        self, directory: str, *, expected_root: str | None
    ) -> SessionRecord:
        async with self.gate:
            if self.activity.active is not None or self.activity.operation is not None:
                raise WorkspaceBusy(self.session_id)
            return await self.operations.relocate_workspace(
                directory, expected_root=expected_root
            )
