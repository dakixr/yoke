"""Workspace recovery at the HTTP execution and resource-ownership seam."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from functools import wraps
from threading import Lock
from typing import TYPE_CHECKING, Concatenate, Protocol

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.mcp.config import McpSessionPolicy
from yoke.session import SessionRecord, SessionStore
from yoke.session.admissions import AdmissionRecord
from yoke.session.queue import prompt_queue_transaction
from yoke.session.workspace_inputs import reconcile_workspace_inputs
from yoke.session.workspace import (
    WorkspaceBusy,
    WorkspaceConflict,
    WorkspaceUnavailable,
    inspect_workspace,
    relocate_session_workspace,
    require_session_workspace,
    require_workspace,
    workspace_lease,
)

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources


class WorkspaceOwner(Protocol):
    store: SessionStore
    session_id: str


def require_runtime_workspace(owner: WorkspaceOwner) -> None:
    record = owner.store.summary_record(owner.session_id)
    if record is None:
        raise ValueError(f"Session not found: {owner.session_id}")
    require_session_workspace(record)


def uses_workspace[Owner: WorkspaceOwner, **P, R](
    function: Callable[Concatenate[Owner, P], R],
) -> Callable[Concatenate[Owner, P], R]:
    """Keep synchronous provider work leased until its actual worker returns."""

    @wraps(function)
    def wrapped(owner: Owner, *args: P.args, **kwargs: P.kwargs) -> R:
        with workspace_lease(owner.store, owner.session_id):
            require_runtime_workspace(owner)
            try:
                return function(owner, *args, **kwargs)
            except (OSError, ValueError) as exc:
                record = owner.store.summary_record(owner.session_id)
                if record is not None:
                    status = inspect_workspace(record.root)
                    if not status.available:
                        raise WorkspaceUnavailable(
                            record.root, session_id=record.id, status=status
                        ) from exc
                raise

    return wrapped


def mutates_workspace[**P, R](
    function: Callable[Concatenate[SessionRuntime, P], Awaitable[R]],
) -> Callable[Concatenate[SessionRuntime, P], Awaitable[R]]:
    """Lease async mutations whose work stays in the coroutine, not a worker."""

    @wraps(function)
    async def wrapped(owner: SessionRuntime, *args: P.args, **kwargs: P.kwargs) -> R:
        with workspace_lease(owner.store, owner.session_id):
            require_runtime_workspace(owner)
            synchronize_runtime_workspace(owner)
            return await function(owner, *args, **kwargs)

    return wrapped


def synchronize_runtime_workspace(runtime: SessionRuntime) -> None:
    """Detect a CLI relocation before reading a cached registry or policy."""
    record = runtime.store.summary_record(runtime.session_id)
    if record is not None:
        synchronize_resource_workspace(runtime.resources, record)


def synchronize_resource_workspace(
    resources: SessionRuntimeResources, record: SessionRecord
) -> None:
    """Retire stale configuration, including policy set before agent startup."""
    with resources.lock:
        if resources._primary_root is None:
            resources._primary_root = record.root
        if resources._primary_root == record.root:
            return
        old = resources._primary_agent
        if resources._process_unsubscribe is not None:
            resources._process_unsubscribe()
            resources._process_unsubscribe = None
        if resources._process_lease is not None:
            resources._process_lease.close()
            resources._process_lease = None
        resources._primary_agent = None
        resources._primary_root = record.root
        resources._session_enabled_tool_names = None
        resources._mcp_session_policy = McpSessionPolicy.empty()
        if old is not None:
            resources._submit_cleanup_locked(old)


def workspace_ready(runtime: SessionRuntime) -> bool:
    """Leave pending work untouched while a directory is unavailable."""
    try:
        require_runtime_workspace(runtime)
    except WorkspaceUnavailable as exc:
        if runtime._active is None and runtime._operation is None:
            runtime._state = "error"
            runtime._last_error = str(exc)
            runtime._best_effort_publish_activity(None)
        return False
    return True


def pause_workspace_input(runtime: SessionRuntime, admission: AdmissionRecord) -> None:
    """Put an input back in the queue if its directory vanished after admission."""
    pending = runtime.pending_inputs
    with prompt_queue_transaction(
        runtime.store.directory, runtime.session_id
    ) as transaction:
        admissions = pending.admissions.load(runtime.session_id)
        current = admissions.records.get(admission.id)
        if current is None or current.settled or current.state == "removed":
            return
        current.workspace_blocked = True
        reconcile_workspace_inputs(
            pending.admissions, runtime.session_id, transaction, snapshot=admissions
        )
        queue = transaction.snapshot
        runtime.events.durable(
            runtime.session_id,
            "session.queue.updated",
            {"revision": queue.revision, "reason": "workspace_unavailable"},
            location=runtime._event_location,
        )


async def reap_with_workspace(
    resources: SessionRuntimeResources, agent: object | None, lease: ExitStack
) -> None:
    """Release use only after cleanup, even if the coroutine waiter is cancelled."""
    completion = resources.retire(agent)
    if completion is None:
        lease.close()
        return
    completion.add_done_callback(lambda _done: lease.close())
    await asyncio.shield(asyncio.wrap_future(completion))


class ProcessWorkspaceLease:
    """Keep relocation excluded while a background command outlives its turn."""

    def __init__(
        self, store: SessionStore, session_id: str, manager: CommandProcessManager
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.manager = manager
        self._lock = Lock()
        self._lease: ExitStack | None = None
        self._closed = False

    def changed(self) -> None:
        with self._lock:
            if self._closed:
                return
            running = any(item.status == "running" for item in self.manager.snapshots())
            if running and self._lease is None:
                lease = ExitStack()
                lease.enter_context(workspace_lease(self.store, self.session_id))
                self._lease = lease
            elif not running and self._lease is not None:
                self._lease.close()
                self._lease = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._lease is not None:
                self._lease.close()
                self._lease = None


async def relocate_runtime_workspace(
    runtime: SessionRuntime, directory: str, *, expected_root: str | None
) -> SessionRecord:
    """Retire root-bound resources under the same gate used by prompt startup."""
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources

    target = str(require_workspace(directory))
    async with runtime._lock:
        record = runtime.store.summary_record(runtime.session_id)
        if record is None:
            raise ValueError(f"Session not found: {runtime.session_id}")
        if record.root == target:
            return record
        if expected_root is not None and expected_root != record.root:
            raise WorkspaceConflict(runtime.session_id, record.root)
        if runtime._active is not None or runtime._operation is not None:
            raise WorkspaceBusy(runtime.session_id)
        if runtime.resources.has_live_work():
            raise WorkspaceBusy(runtime.session_id)
        # Commit only after the cross-process lease and queue checks succeed.
        # A rejected relocation must not discard tools, MCP policy, or providers.
        loop = asyncio.get_running_loop()
        updated = await loop.run_in_executor(
            runtime.executor,
            _relocate_sync,
            runtime,
            target,
            expected_root if expected_root is not None else record.root,
        )
        runtime.cancel_automatic_title()
        # Never retain a closed resource owner, even when the request is cancelled
        # during cleanup. The next runtime must use the committed binding.
        try:
            await runtime.resources.close()
        finally:
            runtime.resources = SessionRuntimeResources(
                session_id=runtime.session_id,
                agent_factory=runtime.agent_factory,
                executor=runtime.executor,
                on_process_change=runtime._on_process_change,
                store=runtime.store,
            )
        runtime._event_location = updated.root
        runtime._state = "idle"
        runtime._last_error = None
        runtime._activity_status = None
        runtime.events.durable(
            runtime.session_id,
            "session.workspace.relocated",
            {
                "sessionID": runtime.session_id,
                "previousDirectory": record.root,
                "directory": updated.root,
            },
            location=updated.root,
        )
        runtime._best_effort_publish_activity(None)
        return updated


def _relocate_sync(
    runtime: SessionRuntime, target: str, expected_root: str | None
) -> SessionRecord:
    # Do not hold a synchronous file/queue lock across an await. Other request
    # handlers use those locks synchronously, which would deadlock the ASGI loop.
    with runtime._persistence_lock:
        return relocate_session_workspace(
            runtime.store, runtime.session_id, target, expected_root=expected_root
        )
