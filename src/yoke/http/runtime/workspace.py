"""Workspace recovery at the HTTP execution and resource-ownership seam."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from functools import wraps
from threading import Lock
from typing import TYPE_CHECKING, Concatenate, Protocol

from yoke.agent.tools.command_process import CommandProcessManager
from yoke.session import SessionStore
from yoke.session.workspace import (
    WorkspaceUnavailable,
    inspect_workspace,
    require_session_workspace,
    workspace_lease,
)

if TYPE_CHECKING:
    pass


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
