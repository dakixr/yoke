"""Own background-process subscriptions and their workspace leases."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.tools.command_process import CommandProcessManager
from yoke.http.runtime.workspace import ProcessWorkspaceLease
from yoke.session import SessionStore


class ResourceProcesses:
    """Retain a workspace binding while background commands outlive a turn."""

    def __init__(
        self, store: SessionStore | None, session_id: str, on_change: Callable[[], None]
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.on_change = on_change
        self._lease: ProcessWorkspaceLease | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def attach(self, manager: CommandProcessManager) -> None:
        if self.store is not None:
            self._lease = ProcessWorkspaceLease(self.store, self.session_id, manager)
            self._lease.changed()
        self._unsubscribe = manager.subscribe(self.changed)

    def changed(self) -> None:
        lease = self._lease
        if lease is not None:
            lease.changed()
        self.on_change()

    def unsubscribe(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def close(self) -> None:
        self.unsubscribe()
        if self._lease is not None:
            self._lease.close()
            self._lease = None
