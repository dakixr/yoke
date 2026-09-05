"""Lifetime and inspection API for managed command processes."""

from __future__ import annotations

import os
import random
import threading
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_support.output import CompletedCommandProcess
from yoke.agent.tools.command_process_support.admission import spawn
from yoke.agent.tools.command_process_types import (
    MAX_COMPLETED_PROCESS_COUNT,
)
from yoke.agent.tools.command_process_types import CancelRequested
from yoke.agent.tools.command_process_types import CommandProcessResult
from yoke.agent.tools.command_process_types import CommandProcessOutputPage
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.agent.tools.command_process_types import clamp_exec_yield_time
from yoke.agent.tools.command_process_types import (
    clamp_write_yield_time,
)

ProcessChangeListener = Callable[[], None]


class CommandProcessManager:
    """Own and expose command processes for one live agent runtime."""

    _managed_process_factory = _ManagedCommandProcess

    def __init__(self, *, base_environment: Mapping[str, str] | None = None) -> None:
        self._lock = threading.RLock()
        self._spawn_lock = threading.Lock()
        self._processes: dict[int, _ManagedCommandProcess] = {}
        self._completed: deque[CompletedCommandProcess] = deque(
            maxlen=MAX_COMPLETED_PROCESS_COUNT
        )
        self._background_session_ids: set[int] = set()
        self._notified_completion_ids: set[int] = set()
        self._completion_events: deque[CommandProcessSnapshot] = deque(
            maxlen=MAX_COMPLETED_PROCESS_COUNT
        )
        self._dropped_completion_events = 0
        self._listeners: set[ProcessChangeListener] = set()
        self._leases = 0
        self._closed = False
        self._base_environment = (
            dict(base_environment) if base_environment is not None else None
        )

    def base_environment(self) -> dict[str, str]:
        """Return the explicit child environment or the current process default."""
        if self._base_environment is None:
            return os.environ.copy()
        return self._base_environment.copy()

    def acquire(self) -> CommandProcessManager:
        """Acquire one runtime lease on this manager."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Command process manager is closed")
            self._leases += 1
        return self

    def release(self) -> None:
        """Release one runtime lease and close after the final release."""
        with self._lock:
            if self._leases == 0:
                return
            self._leases -= 1
            if self._leases:
                return
            self._closed = True
        self.close()

    def exec_command(
        self,
        *,
        command: str,
        cwd: Path,
        tty: bool,
        yield_time_ms: int,
        shell: str | None,
        login: bool,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        """Start a command and return output or a live session reference."""
        managed = self._spawn(command, cwd, tty, shell, login)
        result = managed.wait_and_consume(
            clamp_exec_yield_time(yield_time_ms),
            cancel_requested=cancel_requested,
        )
        if result.session_id is None:
            self._complete(managed.session_id)
        else:
            self._mark_background(managed.session_id)
        return result

    def exec_argv(
        self,
        *,
        argv: list[str],
        display_command: str,
        cwd: Path,
        env: dict[str, str],
        tty: bool = False,
        yield_time_ms: int,
        timeout_seconds: int | None,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        """Start an argv command without shell quoting or interpretation."""
        managed = self._spawn(
            display_command,
            cwd,
            tty,
            None,
            True,
            argv=argv,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        result = managed.wait_and_consume(
            clamp_exec_yield_time(yield_time_ms),
            cancel_requested=cancel_requested,
        )
        if result.session_id is None:
            self._complete(managed.session_id)
        else:
            self._mark_background(managed.session_id)
        return result

    def write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_ms: int | None,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        """Write to or poll a live command session."""
        managed = self._get(session_id)
        if chars:
            try:
                managed.write(chars)
            except (BrokenPipeError, OSError):
                if managed.process.poll() is None:
                    raise
        result = managed.wait_and_consume(
            clamp_write_yield_time(yield_time_ms, has_input=bool(chars)),
            cancel_requested=cancel_requested,
        )
        if result.session_id is None:
            self._complete(session_id)
        return result

    def snapshots(self) -> list[CommandProcessSnapshot]:
        """Return stable non-consuming snapshots, oldest first."""
        with self._lock:
            completed = [process.snapshot for process in self._completed]
            active = [process.snapshot() for process in self._processes.values()]
        return sorted([*completed, *active], key=lambda item: item.started_at)

    def snapshot(self, session_id: int) -> CommandProcessSnapshot:
        """Return one current or retained process snapshot."""
        with self._lock:
            managed = self._processes.get(session_id)
            if managed is not None:
                return managed.snapshot()
            completed = next(
                (
                    item.snapshot
                    for item in self._completed
                    if item.snapshot.session_id == session_id
                ),
                None,
            )
        if completed is None:
            raise ValueError(f"Unknown command session ID {session_id}")
        return completed

    def output_chunks(
        self,
        session_id: int,
        *,
        after_seq: int,
        limit: int,
    ) -> CommandProcessOutputPage:
        """Return a non-consuming retained output page for one process."""
        with self._lock:
            managed = self._processes.get(session_id)
            completed = None
            if managed is None:
                completed = next(
                    (
                        item
                        for item in self._completed
                        if item.snapshot.session_id == session_id
                    ),
                    None,
                )
        if managed is not None:
            return managed.output_page(after_seq=after_seq, limit=limit)
        if completed is None:
            raise ValueError(f"Unknown command session ID {session_id}")
        return completed.output_page(after_seq=after_seq, limit=limit)

    def write_input(self, session_id: int, chars: str) -> None:
        """Write stdin without consuming process output."""
        managed = self._get(session_id)
        managed.write(chars)

    def interrupt(self, session_id: int) -> None:
        """Send the manager's portable interrupt signal to one live process."""
        managed = self._get(session_id)
        managed.interrupt()

    def terminate(self, session_id: int) -> None:
        """Terminate one live process and retain its final inspection snapshot."""
        managed = self._get(session_id)
        managed.terminate()
        self._complete(session_id)

    def completion_events(self) -> list[CommandProcessSnapshot]:
        """Return retained background completions for runtime-local delivery."""
        self._capture_completion_events()
        with self._lock:
            return list(self._completion_events)

    @property
    def dropped_completion_event_count(self) -> int:
        """Return the cumulative number of evicted completion events."""
        with self._lock:
            return self._dropped_completion_events

    def subscribe(self, listener: ProcessChangeListener) -> Callable[[], None]:
        """Subscribe to process output and lifecycle changes."""
        with self._lock:
            if self._closed:
                return lambda: None
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def terminate_all(self) -> int:
        """Terminate all running command sessions."""
        with self._lock:
            processes = list(self._processes.values())
        first_error: BaseException | None = None
        for process in processes:
            try:
                process.terminate()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                continue
            with self._lock:
                if self._processes.get(process.session_id) is process:
                    self._processes.pop(process.session_id)
                    self._background_session_ids.discard(process.session_id)
                    self._notified_completion_ids.discard(process.session_id)
        if processes:
            self._notify()
        if first_error is not None:
            raise first_error
        return len(processes)

    def close(self) -> None:
        """Terminate live processes and release retained process state."""
        with self._lock:
            self._closed = True
            self._listeners.clear()
        first_error: BaseException | None = None
        try:
            self.terminate_all()
        except BaseException as exc:
            first_error = exc
        with self._lock:
            self._completed.clear()
            self._completion_events.clear()
            self._background_session_ids.clear()
            self._notified_completion_ids.clear()
            self._dropped_completion_events = 0
        if first_error is not None:
            raise first_error

    def _spawn(
        self,
        command: str,
        cwd: Path,
        tty: bool,
        shell: str | None,
        login: bool,
        *,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> _ManagedCommandProcess:
        return spawn(
            self,
            command,
            cwd,
            tty,
            shell,
            login,
            argv=argv,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    def _allocate_session_id(self) -> int:
        used = set(self._processes)
        used.update(item.snapshot.session_id for item in self._completed)
        used.update(item.session_id for item in self._completion_events)
        while True:
            candidate = random.SystemRandom().randrange(1_000, 100_000)
            if candidate not in used:
                return candidate

    def _get(self, session_id: int) -> _ManagedCommandProcess:
        with self._lock:
            managed = self._processes.get(session_id)
        if managed is None:
            raise ValueError(f"Unknown command session ID {session_id}")
        return managed

    def _mark_background(self, session_id: int) -> None:
        with self._lock:
            if session_id in self._processes:
                self._background_session_ids.add(session_id)
        self._capture_completion_events()

    def _capture_completion_events(self) -> None:
        with self._lock:
            for session_id in tuple(self._background_session_ids):
                if session_id in self._notified_completion_ids:
                    continue
                managed = self._processes.get(session_id)
                if managed is None or not managed.finished:
                    continue
                self._record_completion_event(managed.snapshot())
                self._notified_completion_ids.add(session_id)

    def _record_completion_event(self, event: CommandProcessSnapshot) -> None:
        if len(self._completion_events) == MAX_COMPLETED_PROCESS_COUNT:
            self._dropped_completion_events += 1
        self._completion_events.append(event)

    def _complete(self, session_id: int) -> None:
        with self._lock:
            managed = self._processes.get(session_id)
            if managed is None:
                return
            completed = managed.completed_record()
        managed.close()
        with self._lock:
            if self._processes.get(session_id) is not managed:
                return
            if (
                session_id in self._background_session_ids
                and session_id not in self._notified_completion_ids
            ):
                self._record_completion_event(completed.snapshot)
            self._processes.pop(session_id)
            self._background_session_ids.discard(session_id)
            self._notified_completion_ids.discard(session_id)
            self._completed.append(completed)
        self._notify()

    def _notify(self) -> None:
        self._capture_completion_events()
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            with suppress(Exception):
                listener()
