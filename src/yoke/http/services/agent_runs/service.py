"""Host-owned registry subscriptions and durable reconnect snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import logging
from threading import Lock, RLock
from typing import Protocol

from yoke.http.errors import ApiError
from yoke.http.models.agent_run import public_snapshot
from yoke.http.services.event_broker import EventService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry

LOGGER = logging.getLogger(__name__)
EVENT = "session.agent.updated"
RETAINED_FINISHED = 256


class RunRegistry(Protocol):
    def snapshots(self) -> list[dict[str, object]]: ...

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]: ...


def _meaningful(info: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in info.items()
        if key not in {"lastSeenAt", "updatedAt", "version"}
    }


class AgentRunService:
    """Use the existing journal writer, with no SDK-side durable writes."""

    def __init__(self, runtimes: SessionRuntimeRegistry, events: EventService) -> None:
        self.runtimes = runtimes
        self.events = events
        self._lock = RLock()
        self._reconcile_lock = Lock()
        self._rows: dict[str, dict[str, dict[str, object]]] = {}
        self._subscriptions: dict[str, tuple[RunRegistry, Callable[[], None]]] = {}
        self._revisions: dict[str, int] = {}
        self._source_versions: dict[str, dict[str, int]] = {}
        self._public_versions: dict[str, int] = {}
        self._closed = False

    async def watch(self) -> None:
        """Discover managers created during foreground turns, even without reads."""
        while True:
            await asyncio.to_thread(self.reconcile)
            await asyncio.sleep(0.5)

    def reconcile(self) -> None:
        """Subscribe before reading; periodically repair missed notifications."""
        # Concurrent HTTP reads and the watcher must not apply stale discovery
        # results after a newer reconciler has subscribed to a live registry.
        with self._reconcile_lock:
            self._reconcile()

    def _reconcile(self) -> None:
        try:
            loaded = self.runtimes.loaded_runtimes()
            current = {}
            for session_id, runtime in loaded:
                manager = runtime.process_manager()
                registry = getattr(manager, "agent_runs", None)
                if registry is not None:
                    current[session_id] = registry
            with self._lock:
                if self._closed:
                    return
                for session_id, (registry, unsubscribe) in list(
                    self._subscriptions.items()
                ):
                    if current.get(session_id) is not registry:
                        unsubscribe()
                        del self._subscriptions[session_id]
                        self._revisions.pop(session_id, None)
                        self._source_versions.pop(session_id, None)
                        self._lose(session_id)
                for session_id, registry in current.items():
                    if session_id not in self._subscriptions:
                        self._recover(session_id)
                        unsubscribe = registry.subscribe(
                            lambda owner=session_id, source=registry: self._changed(
                                owner, source
                            )
                        )
                        self._subscriptions[session_id] = (registry, unsubscribe)
                    self._changed(session_id, registry)
        except Exception:  # noqa: BLE001 - observation must not fail a real turn
            LOGGER.exception("Agent registry reconciliation failed")

    def _changed(self, session_id: str, registry: RunRegistry) -> None:
        try:
            with self._lock:
                subscribed = self._subscriptions.get(session_id)
                if self._closed or subscribed is None or subscribed[0] is not registry:
                    return
                rows = self._rows[session_id]
                source_versions = self._source_versions.setdefault(session_id, {})
                revision = self._revisions.get(session_id, -1)
                newest = revision
                observed: set[str] = set()
                for value in registry.snapshots():
                    info = public_snapshot(value, session_id)
                    if info is None:
                        continue
                    run_id = str(info["runId"])
                    observed.add(run_id)
                    version = int(str(info["version"]))
                    newest = max(newest, version)
                    previous = rows.get(run_id)
                    if previous is None and version <= revision:
                        continue  # Already journaled, then removed by retention.
                    if version < source_versions.get(run_id, -1):
                        continue
                    if previous is not None and version == source_versions.get(run_id):
                        comparable = {**previous, "observation": info["observation"]}
                        if _meaningful(comparable) != _meaningful(info):
                            continue
                    # HTTP observation loss has its own revision. It must not
                    # consume a future SDK revision or mask a real completion.
                    if (
                        previous is not None
                        and previous["status"] != "running"
                        and info["status"] == "running"
                    ):
                        continue
                    changed = previous is None or _meaningful(previous) != _meaningful(
                        info
                    )
                    if changed:
                        info["version"] = self._next_version(session_id, version)
                        self._persist(session_id, info)
                    elif previous is not None:
                        info["version"] = previous["version"]
                    rows[run_id] = info
                    source_versions[run_id] = version
                self._revisions[session_id] = newest
                # Notifications can coalesce a run's terminal transition and
                # subsequent retention eviction. Absence cannot prove success,
                # but must retire the last observed claim of live execution.
                self._lose(session_id, missing_from=observed)
                self._trim(rows)
                for run_id in source_versions.keys() - rows.keys():
                    source_versions.pop(run_id)
        except Exception:  # noqa: BLE001 - registry callbacks cannot fail SDK calls
            LOGGER.exception("Agent registry observation failed")

    def _persist(self, session_id: str, info: dict[str, object]) -> None:
        self.events.durable(session_id, EVENT, {"snapshot": info})

    def _next_version(self, session_id: str, source_version: int = 0) -> int:
        version = max(source_version, self._public_versions.get(session_id, 0) + 1)
        self._public_versions[session_id] = version
        return version

    @staticmethod
    def _trim(rows: dict[str, dict[str, object]]) -> None:
        finished = sorted(
            (
                item
                for item in rows.values()
                if item["status"] != "running" or item["observation"] == "lost"
            ),
            key=lambda item: str(item["startedAt"]),
            reverse=True,
        )
        for info in finished[RETAINED_FINISHED:]:
            rows.pop(str(info["runId"]), None)

    def _recover(self, session_id: str) -> None:
        if session_id in self._rows:
            return
        rows: dict[str, dict[str, object]] = {}
        after = 0
        while True:
            page, more = self.events.journal.history(session_id, after=after, limit=256)
            for event in page:
                after = event.seq
                if event.type != EVENT:
                    continue
                info = public_snapshot(event.data.get("snapshot"), session_id)
                if info is not None:
                    rows[str(info["runId"])] = info
                    self._public_versions[session_id] = max(
                        self._public_versions.get(session_id, 0),
                        int(str(info["version"])),
                    )
            self._trim(rows)
            if not more:
                break
        self._rows[session_id] = rows
        # Nothing loaded from a previous daemon proves that execution still lives.
        self._lose(session_id)

    def _lose(self, session_id: str, *, missing_from: set[str] | None = None) -> None:
        rows = self._rows.get(session_id, {})
        for run_id, previous in list(rows.items()):
            if missing_from is not None and run_id in missing_from:
                continue
            if previous["status"] != "running" or previous["observation"] == "lost":
                continue
            info = {
                **previous,
                "observation": "lost",
                "version": self._next_version(session_id),
                "updatedAt": datetime.now(UTC).isoformat(),
            }
            self._persist(session_id, info)
            rows[run_id] = info
        self._trim(rows)

    def snapshots(self, session_id: str) -> list[dict[str, object]]:
        """Validate the requested session before touching its journal path."""
        if self.runtimes.store.index_entry(session_id) is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        self.reconcile()
        with self._lock:
            self._recover(session_id)
            return sorted(
                (dict(info) for info in self._rows[session_id].values()),
                key=lambda item: str(item["startedAt"]),
                reverse=True,
            )

    def close(self) -> None:
        """Release listeners without closing registries or stopping jobs."""
        with self._lock:
            self._closed = True
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
        for _, unsubscribe in subscriptions:
            unsubscribe()
