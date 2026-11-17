"""Lifetime and inspection API for managed command processes."""

from __future__ import annotations

import os
import random
import threading
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path

from yoke.agent.tools.command_process import admission
from yoke.agent.tools.command_process.agent_runs import prepare_launch
from yoke.agent.tools.command_process.coordination import acquired, check_cancelled
from yoke.agent.tools.python_env import prepare_python_env
from yoke.agent.tools.shell import build_shell_command
from yoke.agent_runs.context import Binding
from yoke.ai.providers.usage_attribution import attribute_subprocess_environment
from yoke.agent.tools.command_process.output import CompletedCommandProcess
from yoke.agent.tools.command_process.process import _ManagedCommandProcess
from yoke.agent.tools.command_process.types import (
    MAX_COMPLETED_PROCESS_COUNT,
    MAX_PROCESS_COUNT,
    CancelRequested,
    CommandProcessOutputPage,
    CommandProcessResult,
    CommandProcessSnapshot,
    clamp_exec_yield_time,
    clamp_write_yield_time,
)
from yoke.agent_runs import AgentRunRegistry

ProcessChangeListener = Callable[[], None]


class CommandProcessManager:
    """Own and expose command processes for one live agent runtime."""

    _managed_process_factory = _ManagedCommandProcess

    def __init__(self, *, base_environment: Mapping[str, str] | None = None) -> None:
        self.agent_runs = AgentRunRegistry()
        self._lock = threading.RLock()
        self._spawn_lock = threading.Lock()
        self._next_session_id = random.SystemRandom().randrange(1_000, 100_000)
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
        process = self._retained_process(session_id)
        if isinstance(process, CompletedCommandProcess):
            return process.snapshot
        return process.snapshot()

    def output_chunks(
        self,
        session_id: int,
        *,
        after_seq: int,
        limit: int,
        exact: bool = False,
    ) -> CommandProcessOutputPage:
        """Return a non-consuming retained output page for one process."""
        return self._retained_process(session_id).output_page(
            after_seq=after_seq, limit=limit, exact=exact
        )

    def observe_output(
        self, session_id: int, *, after_seq: int, limit: int
    ) -> tuple[CommandProcessSnapshot, CommandProcessOutputPage]:
        """Read coherent status and history without copying the entire output tail."""
        process = self._retained_process(session_id)
        if isinstance(process, CompletedCommandProcess):
            return process.snapshot, process.output_page(
                after_seq=after_seq, limit=limit, exact=True
            )
        with process.condition:
            return (
                process._snapshot_locked(include_output=False),
                process.output.page(after_seq=after_seq, limit=limit, exact=True),
            )

    def _retained_process(
        self, session_id: int
    ) -> _ManagedCommandProcess | CompletedCommandProcess:
        with self._lock:
            if self._closed:
                raise RuntimeError("Command process manager is closed")
            managed = self._processes.get(session_id)
            if managed is not None:
                return managed
            completed = next(
                (
                    item
                    for item in self._completed
                    if item.snapshot.session_id == session_id
                ),
                None,
            )
        if completed is None:
            raise ValueError(f"Unknown command session ID {session_id}")
        return completed

    def write_input(
        self,
        session_id: int,
        chars: str,
        *,
        cancel_requested: CancelRequested | None = None,
    ) -> int:
        """Write stdin without consuming process output."""
        managed = self._get(session_id)
        return managed.write(chars, cancel_requested=cancel_requested)

    def interrupt(self, session_id: int) -> None:
        """Send the manager's portable interrupt signal to one live process."""
        managed = self._get(session_id)
        managed.interrupt()

    def terminate(self, session_id: int) -> None:
        """Terminate one live process and retain its final inspection snapshot."""
        managed = self._retained_process(session_id)
        if isinstance(managed, CompletedCommandProcess):
            return
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
            listeners = tuple(self._listeners)
            self._listeners.clear()
        first_error: BaseException | None = None
        try:
            self.terminate_all()
        except BaseException as exc:
            first_error = exc
        self.agent_runs.close()
        with self._lock:
            self._completed.clear()
            self._completion_events.clear()
            self._background_session_ids.clear()
            self._notified_completion_ids.clear()
            self._dropped_completion_events = 0
        for listener in listeners:
            with suppress(Exception):
                listener()
        if first_error is not None:
            raise first_error

    def allocate_session_id(self) -> int:
        """Allocate a JSON-safe handle without ever recycling retired runtime IDs."""
        used = set(self._processes)
        used.update(item.snapshot.session_id for item in self._completed)
        used.update(item.session_id for item in self._completion_events)
        while self._next_session_id in used:
            self._next_session_id += 1
        if self._next_session_id >= 2**53:
            raise RuntimeError("Process session ID space exhausted")
        session_id = self._next_session_id
        self._next_session_id += 1
        return session_id

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
        cancel_requested: CancelRequested | None = None,
    ) -> _ManagedCommandProcess:
        values = argv if argv is not None else [command, shell or ""]
        if any("\x00" in value for value in values):
            raise ValueError("Process arguments must not contain NUL characters")
        with acquired(self._spawn_lock, cancel_requested):
            self._prune()
            managed: _ManagedCommandProcess | None = None
            master_fd: int | None = None
            slave_fd: int | None = None
            launch_binding: Binding | None = None
            try:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("Command process manager is closed")
                    check_cancelled(cancel_requested)
                    session_id = self.allocate_session_id()
                    process_env = (
                        env.copy() if env is not None else self.base_environment()
                    )
                    attribute_subprocess_environment(process_env)
                    launch_binding = prepare_launch(self, process_env, session_id)
                    if env is None:
                        prepare_python_env(process_env)
                    process_argv = argv or build_shell_command(
                        command, process_env, shell=shell, login=login
                    )
                    check_cancelled(cancel_requested)
                    process, master_fd, slave_fd = admission.open_process(
                        process_argv, cwd, process_env, tty=tty
                    )
                    managed = self._managed_process_factory(
                        session_id=session_id,
                        command=command,
                        cwd=cwd,
                        process=process,
                        tty=tty,
                        master_fd=master_fd,
                        on_change=self._notify,
                        timeout_seconds=timeout_seconds,
                    )
                    if launch_binding is None:
                        pass  # Reporting outage, child SDK admission remains mandatory.
                    elif launch_binding.registry is self.agent_runs:
                        # A shell can exit while its owned descendants still run.
                        # Retire authority after existing tree cleanup, not shell exit.
                        self.agent_runs.watch_launch(
                            launch_binding.token,
                            lambda owned=managed: 0 if owned.closed else None,
                        )
                    else:
                        with suppress(Exception):
                            launch_binding.attach(process.pid)
                        self.agent_runs.watch_remote_launch(
                            lambda owned=managed: owned.closed, launch_binding.revoke
                        )
                    self._processes[session_id] = managed
                    managed.start_readers()
            except BaseException:
                if launch_binding is not None:
                    with suppress(Exception):
                        launch_binding.revoke()
                if managed is not None:
                    try:
                        managed.terminate()
                    except BaseException:
                        # Keep failed cleanup owned by the manager for a later retry.
                        pass
                    else:
                        with self._lock:
                            self._processes.pop(managed.session_id, None)
                elif master_fd is not None:
                    os.close(master_fd)
                raise
            finally:
                if slave_fd is not None:
                    os.close(slave_fd)
        self._notify()
        return managed

    def _prune(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Command process manager is closed")
            if len(self._processes) < MAX_PROCESS_COUNT:
                return
            oldest = min(
                self._processes.values(),
                key=lambda process: (not process.finished, process.last_used_at),
            )
        if oldest.finished:
            self._complete(oldest.session_id)
        else:
            raise RuntimeError(
                "Process capacity reached; finish work or use process_cancel before starting another process"
            )

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
