"""One observation registry over independent client process managers."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from yoke.agent.tools.command_process import CommandProcessManager


class SessionAgentRuns:
    """Merge snapshots without sharing ownership of a manager's SDK registry."""

    def __init__(self, managers: Callable[[], list[CommandProcessManager]]) -> None:
        self.managers = managers
        self._subscriptions: dict[int, Callable[[], None]] = {}
        self._listeners: set[Callable[[], None]] = set()
        self._versions: dict[tuple[int, str], tuple[object, int]] = {}
        self._revision = 0
        self._lock = RLock()

    def refresh(self) -> None:
        sources = {
            id(manager.agent_runs): manager.agent_runs for manager in self.managers()
        }
        with self._lock:
            for identity in self._subscriptions.keys() - sources.keys():
                self._subscriptions.pop(identity)()
            for identity, source in sources.items():
                if identity not in self._subscriptions:
                    self._subscriptions[identity] = source.subscribe(self.changed)

    def snapshots(self) -> list[dict[str, object]]:
        result = []
        for manager in self.managers():
            source = manager.agent_runs
            for snapshot in source.snapshots():
                key = (id(source), str(snapshot["runId"]))
                with self._lock:
                    previous = self._versions.get(key)
                    if previous is None or previous[0] != snapshot["version"]:
                        self._revision += 1
                        self._versions[key] = (snapshot["version"], self._revision)
                    version = self._versions[key][1]
                result.append({**snapshot, "version": version})
        return result

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(callback)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(callback)

        return unsubscribe

    def changed(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener()

    def close(self) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
            self._listeners.clear()
        for unsubscribe in subscriptions:
            unsubscribe()
