"""Thread-safe host authority and bounded SDK run history."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
import os
import secrets
import threading
import time

from yoke.agent_runs.records import (
    Capability,
    Owner,
    Record,
    metadata,
    text,
    typed_usage,
)
from yoke.agent_runs.capabilities import delegate, retention_token
from yoke.agent_runs.lifetime import Lifetime
from yoke.agent_runs.notifications import Notifier
from yoke.agent_runs.server import Server
from yoke.agent_runs.worker import Worker

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class AgentRunRegistry:
    """One runtime's launch capabilities and non-durable run observations."""

    def __init__(
        self,
        *,
        history_limit: int = 256,
        stale_after: float = 15,
        lost_after: float = 45,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, Record] = {}
        self._capabilities: dict[str, Capability] = {}
        self._launch_cleanup: list[tuple[Callable[[], bool], Callable[[], None]]] = []
        self._notifier = Notifier()
        self._revision = 0
        self._closed = False
        self._authority_key = secrets.token_bytes(32)
        self._history_limit = max(0, history_limit)
        self._clock = clock
        self._stale_after = stale_after
        self._lost_after = lost_after
        self._server: Server | None = None
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()

    def snapshots(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                deepcopy(item.snapshot) for item in reversed(self._records.values())
            ]

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._notifier.subscribe(callback)

    def capability(self, owner: Owner) -> str:
        with self._lock:
            self._ensure_open()
            token = secrets.token_urlsafe(32)
            self._capabilities[token] = Capability(
                owner, lifetime=Lifetime(), owns_lifetime=True
            )
            return token

    def revoke(self, token: str) -> None:
        with self._lock:
            capability = self._capabilities.pop(token, None)
            if capability is not None and capability.owns_lifetime:
                assert capability.lifetime is not None
                capability.lifetime.close()

    def launch(
        self,
        parent_token: str,
        runtime_session_id: int | None = None,
        *,
        pid: int | None = None,
        process_launch: bool = True,
        request_id: str | None = None,
    ) -> str:
        """Retain launch lineage independently of a parent's public snapshot."""
        with self._lock:
            self._ensure_open()
            token = delegate(
                self._capabilities,
                self._authority_key,
                parent_token,
                runtime_session_id=runtime_session_id,
                pid=pid,
                process_launch=process_launch,
                request_id=request_id,
            )
            self._start_monitor()
            return token

    def release_retained(self, parent: str, request_id: str, *, pid: int) -> None:
        """Release known or unacknowledged authority, even after its parent expired."""
        self.revoke(retention_token(self._authority_key, parent, pid, request_id))

    def watch_launch(self, token: str, poll: Callable[[], int | None]) -> None:
        with self._lock:
            if token in self._capabilities:
                lifetime = self._capabilities[token].lifetime
                if lifetime is None:
                    lifetime = Lifetime()
                    self._capabilities[token].lifetime = lifetime
                    self._capabilities[token].owns_lifetime = True
                lifetime.poll = poll
            self._start_monitor()

    def watch_remote_launch(
        self, closed: Callable[[], bool], revoke: Callable[[], None]
    ) -> None:
        """Retire forwarded authority after this manager cleans up its process tree."""
        with self._lock:
            self._ensure_open()
            self._launch_cleanup.append((closed, revoke))
            self._start_monitor()

    def _retire_launches(self, *, closing: bool = False) -> None:
        with self._lock:
            retired = [item for item in self._launch_cleanup if closing or item[0]()]
            self._launch_cleanup = [
                item for item in self._launch_cleanup if item not in retired
            ]
        for _, revoke in retired:
            with suppress(Exception):
                revoke()

    def address(self) -> str:
        with self._lock:
            self._ensure_open()
            if self._server is None:
                self._server = Server(self)
            return self._server.address

    def bind(self, *, session_id: str | None = None):
        """Bind trusted host ownership for in-process SDK calls in this context."""
        from yoke.agent_runs.context import bind_host

        return bind_host(self, session_id=session_id)

    def dispatch(
        self,
        frame: dict[str, object],
        *,
        pid: int | None = None,
    ) -> dict[str, object]:
        peer = os.getpid() if pid is None else pid
        with self._lock:
            self._ensure_open()
            token = text(frame.get("token"), required=True)
            if frame.get("op") == "revoke":
                self.revoke(token or "")
                return {}  # Replayed release after a lost ACK is idempotent.
            if frame.get("op") == "release_retained":
                self.release_retained(
                    token or "",
                    text(frame.get("requestId"), required=True) or "",
                    pid=peer,
                )
                return {}
            if frame.get("op") in {"launch", "retain"}:
                return {
                    "token": self.launch(
                        token or "",
                        pid=peer,
                        process_launch=frame["op"] == "launch",
                        request_id=text(frame.get("requestId")),
                    )
                }
            capability = self._capabilities.get(token or "")
            if capability is None:
                raise ValueError("Invalid launch capability")
            if frame.get("op") == "register":
                result, changed = self._register(frame, token or "", capability, peer)
            elif frame.get("op") == "attach":
                child = frame.get("childPid")
                if type(child) is not int or child <= 0 or capability.lifetime is None:
                    raise ValueError("Invalid launch process")
                capability.lifetime.attach(child, peer)
                return {}
            elif frame.get("op") == "report" and capability.run_id is not None:
                changed = self._report(frame, self._records[capability.run_id], peer)
                result = {}
            else:
                raise ValueError("Invalid reporting operation")
            self._trim()
        if changed:
            self._notifier.notify()
        return result

    def _register(
        self,
        frame: dict[str, object],
        token: str,
        capability: Capability,
        pid: int,
    ) -> tuple[dict[str, object], bool]:
        values = metadata(frame)
        run_id = str(values["runId"])
        existing = self._records.get(run_id)
        if existing is not None:
            if (
                existing.launch_token != token
                or existing.metadata != values
                or existing.worker.pid != pid
            ):
                raise ValueError("Run identity conflict")
            return {"token": existing.token}, False
        owner = capability.owner
        if capability.lifetime is not None:
            capability.lifetime.observe(pid)
        worker = Worker.open(pid)
        now = self._timestamp()
        run_token = secrets.token_urlsafe(32)
        snapshot: dict[str, object] = {
            "schemaVersion": 1,
            **values,
            "sessionID": owner.session_id,
            "runtimeSessionID": owner.runtime_session_id,
            "parentRunId": owner.parent_run_id,
            "parentAgentId": owner.parent_agent_id,
            "status": "running",
            "observation": "live",
            "startedAt": now,
            "finishedAt": None,
            "lastSeenAt": now,
            "updatedAt": now,
            "version": 0,
            "lastToolName": None,
            "errorType": None,
        }
        record = Record(
            snapshot, token, run_token, worker, self._clock(), metadata=values
        )
        self._records[run_id] = record
        self._capabilities[run_token] = Capability(
            Owner(
                owner.session_id,
                owner.runtime_session_id,
                run_id,
                str(values["agentId"]),
            ),
            run_id=run_id,
            lifetime=capability.lifetime,
        )
        self._changed(record)
        self._start_monitor()
        return {"token": run_token}, True

    def _report(self, frame: dict[str, object], record: Record, pid: int) -> bool:
        sequence = frame.get("sequence")
        if record.worker.pid != pid or type(sequence) is not int or sequence < 1:
            raise ValueError("Invalid run reporter")
        if sequence <= record.sequence or record.snapshot["status"] != "running":
            return False
        status = frame.get("status", "running")
        if status not in TERMINAL | {"running"}:
            raise ValueError("Invalid run status")
        updates: dict[str, object] = {"status": status, "observation": "live"}
        for key in ("lastToolName", "errorType"):
            if key in frame:
                updates[key] = text(frame[key])
        if "typedUsage" in frame:
            updates["typedUsage"] = typed_usage(frame["typedUsage"])
        record.sequence = sequence
        record.seen = self._clock()
        record.snapshot["lastSeenAt"] = self._timestamp()
        changed = any(
            record.snapshot.get(key) != value for key, value in updates.items()
        )
        record.snapshot.update(updates)
        if status in TERMINAL:
            self._finish(record)
        if changed:
            self._changed(record)
        return changed

    def reconcile(self) -> None:
        """Check real worker death and heartbeat age. Also usable with a fake clock."""
        self._retire_launches()
        changed = False
        with self._lock:
            if self._closed:
                return
            for token, capability in tuple(self._capabilities.items()):
                if (
                    capability.owns_lifetime
                    and capability.lifetime is not None
                    and capability.lifetime.dead()
                ):
                    self.revoke(token)
            for record in self._records.values():
                if record.snapshot["status"] != "running":
                    continue
                age = self._clock() - record.seen
                observation = (
                    "lost"
                    if age >= self._lost_after
                    else "stale"
                    if age >= self._stale_after
                    else "live"
                )
                if record.worker.dead():
                    record.snapshot.update(status="interrupted", observation="lost")
                    self._finish(record)
                    self._changed(record)
                    changed = True
                elif record.snapshot["observation"] != observation:
                    record.snapshot["observation"] = observation
                    self._changed(record)
                    changed = True
            self._trim()
        if changed:
            self._notifier.notify()

    def _finish(self, record: Record) -> None:
        record.snapshot["finishedAt"] = self._timestamp()
        record.terminal_at = self._clock()
        record.worker.close()

    def _changed(self, record: Record) -> None:
        self._revision += 1
        record.snapshot.update(version=self._revision, updatedAt=self._timestamp())

    def _trim(self) -> None:
        completed = [
            item for item in self._records.items() if item[1].terminal_at is not None
        ]
        completed.sort(key=lambda item: item[1].terminal_at or 0)
        for run_id, record in completed[: max(0, len(completed) - self._history_limit)]:
            self._records.pop(run_id)
            self._capabilities.pop(record.token, None)
            record.worker.close()

    def _start_monitor(self) -> None:
        if self._monitor is None:
            self._monitor = threading.Thread(
                target=self._monitor_main, daemon=True, name="yoke-agent-run-watch"
            )
            self._monitor.start()

    def _monitor_main(self) -> None:
        while not self._stop.wait(1):
            with suppress(Exception):
                self.reconcile()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Agent run registry is closed")

    def close(self) -> None:
        self.reconcile()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            for record in self._records.values():
                if record.snapshot["status"] == "running":
                    # Registry shutdown is loss of observation, not proof of death.
                    record.snapshot["observation"] = "lost"
                    self._changed(record)
                record.worker.close()
            for token in tuple(self._capabilities):
                self.revoke(token)
        if self._server is not None:
            self._server.close()
        if (
            self._monitor is not None
            and self._monitor is not threading.current_thread()
        ):
            self._monitor.join(timeout=1)
        self._retire_launches(closing=True)
        self._notifier.close()
