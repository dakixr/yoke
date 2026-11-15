"""The asyncio admission and lifecycle gate for one HTTP-owned session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime
from functools import partial
from threading import Event
from time import monotonic

from yoke.agent.observability import ToolTraceStore
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.http.models.session import ActiveRuntimeInfo
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.runtime_factory import SessionAgentFactory
from yoke.http.services.runtime_title import SessionTitleAutomation
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.http.services.session_runtime.agent_events import AgentEvents
from yoke.http.services.session_runtime.configuration import RuntimeConfiguration
from yoke.http.services.session_runtime.controller import TurnController
from yoke.http.services.session_runtime.execution import (
    ReservedInput,
    TurnExecution,
)
from yoke.http.services.session_runtime.finalize import TurnFinalizer
from yoke.http.services.session_runtime.inputs import RuntimeInputs
from yoke.http.services.session_runtime.operations import SessionOperations
from yoke.http.services.session_runtime.persistence import TurnPersistence
from yoke.http.services.session_runtime.resources import SessionRuntimeResources
from yoke.http.services.session_runtime.status import RuntimeStatus
from yoke.http.services.session_runtime.turn_worker import TurnWorker
from yoke.http.services.session_runtime.workers import WorkerTracker
from yoke.http.services.session_runtime.workspace import (
    require_runtime_workspace,
)
from yoke.mcp.config import McpSessionPolicy
from yoke.session import SessionRecord, SessionStore
from yoke.session.admissions import AdmissionRecord
from yoke.session.workspace import WorkspaceBusy, WorkspaceUnavailable, workspace_lease

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
        self.active_slots = active_slots
        self.gate = asyncio.Lock()
        self.resources = SessionRuntimeResources(
            session_id=session_id,
            agent_factory=agent_factory,
            executor=executor,
            on_process_change=self.on_process_change,
            store=store,
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
            agent_factory=agent_factory,
            executor=executor,
            persistence_lock=self.persistence.lock,
            user_message_factory=self.persistence.user_message,
        )
        self._turn_counter = 0
        self.inputs = RuntimeInputs(self)
        self.configuration = RuntimeConfiguration(self)
        self.operations = SessionOperations(self)
        self.agent_events = AgentEvents(self)
        self.turn_worker = TurnWorker(self)
        self.finalizer = TurnFinalizer(self)
        self.controller = TurnController(self)
        self.select_model = self.operations.select_model
        self.compact = self.operations.compact
        self.activate_skill = self.configuration.activate_skill
        self.set_tools = self.configuration.set_tools
        self.set_mcp_policy = self.configuration.set_mcp_policy

    async def wake(self) -> None:
        """Start eligible work or apply a pending steer at a safe control boundary."""
        async with self.gate:
            try:
                await self.run_blocking(require_runtime_workspace, self)
            except WorkspaceUnavailable as exc:
                if self.activity.active is None and self.activity.operation is None:
                    self.activity.state = "error"
                    self.activity.last_error = str(exc)
                    self.activity.best_effort_publish_activity(None)
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

    async def reserve_input(
        self, *, allow_queue: bool = True, recover: bool = True
    ) -> ReservedInput | None:
        """Recover input on a worker and report workspace failures on the loop."""
        try:
            return await self.run_blocking(
                self.inputs.reserve, allow_queue=allow_queue, recover=recover
            )
        except WorkspaceUnavailable as exc:
            if self.activity.active is None and self.activity.operation is None:
                self.activity.state = "error"
                self.activity.last_error = str(exc)
                self.activity.best_effort_publish_activity(None)
            return None

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
        async with self.gate:
            if self.activity.active is None:
                return False, None
            turn_id = self.activity.active.turn_id
            await self.finalizer.retire(reason="user")
            return True, turn_id

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
                if task is None:
                    await asyncio.sleep(0)
                else:
                    await asyncio.shield(task)

        if timeout_seconds is None:
            await wait_idle()
        else:
            await asyncio.wait_for(wait_idle(), timeout_seconds)
        return await self.status()

    async def status(self) -> ActiveRuntimeInfo:
        """Return process-local activity without loading durable session metadata."""
        async with self.gate:
            active = self.activity.active
            operation = self.activity.operation
            return ActiveRuntimeInfo(
                state=self.activity.state,
                turn_id=active.turn_id if active is not None else None,
                started_at=(
                    active.started_at
                    if active is not None
                    else operation.started_at
                    if operation is not None
                    else None
                ),
                activity=self.activity.activity,
                last_error=self.activity.last_error,
            )

    async def idle_mutation[T](self, mutation: Callable[[], T]) -> T:
        """Run a synchronous session mutation while prompt starts are excluded."""

        def mutate() -> T:
            with workspace_lease(self.store, self.session_id):
                self.resources.synchronize_workspace()
                return mutation()

        async with self.gate:
            self.activity.require_idle()
            return await self.run_blocking(mutate)

    async def run_blocking[T, **P](
        self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, partial(function, *args, **kwargs)
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise

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
                self.activity.active = None
                self.activity.state = "error"
                self.activity.last_error = str(exc)
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
        )
        self.activity.active = execution
        self.activity.state = "running"
        self.activity.last_error = None
        self.activity.activity = "Loading session" if cold_start else "Thinking"
        self.activity.tool_call_ids = {execution.turn_id: set()}
        self.activity.publish_activity(execution)
        self.controller.launch(execution)
        return execution

    def on_process_change(self) -> None:
        self.events.live(
            "session.process.updated",
            {},
            session_id=self.session_id,
            location=self.activity.event_location,
        )

    def release_slot(self, execution: TurnExecution) -> None:
        if execution.slot_acquired and not execution.slot_released:
            execution.slot_released = True
            self.active_slots.release()

    async def relocate_workspace(
        self, directory: str, *, expected_root: str | None
    ) -> SessionRecord:
        async with self.gate:
            if self.activity.active is not None or self.activity.operation is not None:
                raise WorkspaceBusy(self.session_id)
            return await self.configuration.relocate_workspace(
                directory, expected_root=expected_root
            )
