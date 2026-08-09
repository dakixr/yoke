"""Lifetime and inspection API for managed command processes."""

from __future__ import annotations

import os
import random
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_types import (
    MAX_COMPLETED_PROCESS_COUNT,
)
from yoke.agent.tools.command_process_types import MAX_PROCESS_COUNT
from yoke.agent.tools.command_process_types import CancelRequested
from yoke.agent.tools.command_process_types import CommandProcessResult
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.agent.tools.command_process_types import clamp_exec_yield_time
from yoke.agent.tools.command_process_types import (
    clamp_write_yield_time,
)
from yoke.agent.tools.python_env import prepare_python_env
from yoke.agent.tools.shell import build_shell_command

ProcessChangeListener = Callable[[], None]


class CommandProcessManager:
    """Own and expose command processes for one live agent runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[int, _ManagedCommandProcess] = {}
        self._completed: deque[CommandProcessSnapshot] = deque(
            maxlen=MAX_COMPLETED_PROCESS_COUNT
        )
        self._listeners: set[ProcessChangeListener] = set()
        self._leases = 0
        self._closed = False

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
            if self._leases > 0:
                self._leases -= 1
                if self._leases == 0:
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
        return result

    def exec_argv(
        self,
        *,
        argv: list[str],
        display_command: str,
        cwd: Path,
        env: dict[str, str],
        yield_time_ms: int,
        timeout_seconds: int | None,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        """Start an argv command without shell quoting or interpretation."""
        managed = self._spawn(
            display_command,
            cwd,
            False,
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
            completed = list(self._completed)
            active = [process.snapshot() for process in self._processes.values()]
        return sorted([*completed, *active], key=lambda item: item.started_at)

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
            self._processes.clear()
        for process in processes:
            process.terminate()
        if processes:
            self._notify()
        return len(processes)

    def close(self) -> None:
        """Terminate live processes and release retained process state."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._listeners.clear()
        self.terminate_all()
        with self._lock:
            self._completed.clear()

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
        with self._lock:
            if self._closed:
                raise RuntimeError("Command process manager is closed")
            self._prune_if_needed()
            session_id = self._allocate_session_id()
            process_env = env.copy() if env is not None else os.environ.copy()
            if env is None:
                prepare_python_env(process_env)
            process_argv = argv or build_shell_command(
                command, process_env, shell=shell, login=login
            )
            master_fd: int | None = None
            slave_fd: int | None = None
            managed: _ManagedCommandProcess | None = None
            try:
                process, master_fd, slave_fd = _open_process(
                    process_argv, cwd, process_env, tty=tty
                )
                managed = _ManagedCommandProcess(
                    session_id=session_id,
                    command=command,
                    cwd=cwd,
                    process=process,
                    tty=tty,
                    master_fd=master_fd,
                    on_change=self._notify,
                    timeout_seconds=timeout_seconds,
                )
                self._processes[session_id] = managed
                managed.start_readers()
            except Exception:
                self._processes.pop(session_id, None)
                if managed is not None:
                    managed.terminate()
                elif master_fd is not None:
                    os.close(master_fd)
                raise
            finally:
                if slave_fd is not None:
                    os.close(slave_fd)
        self._notify()
        return managed

    def _allocate_session_id(self) -> int:
        used = set(self._processes)
        used.update(item.session_id for item in self._completed)
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

    def _complete(self, session_id: int) -> None:
        with self._lock:
            managed = self._processes.pop(session_id, None)
            if managed is None:
                return
            self._completed.append(managed.snapshot())
        managed.close()
        self._notify()

    def _prune_if_needed(self) -> None:
        if len(self._processes) < MAX_PROCESS_COUNT:
            return
        oldest = min(
            self._processes.values(),
            key=lambda process: (not process.finished, process.last_used_at),
        )
        self._processes.pop(oldest.session_id, None)
        if oldest.finished:
            self._completed.append(oldest.snapshot())
            oldest.close()
        else:
            oldest.terminate()

    def _notify(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            with suppress(Exception):
                listener()


def _open_process(
    argv: list[str] | str, cwd: Path, env: dict[str, str], *, tty: bool
) -> tuple[subprocess.Popen[bytes], int | None, int | None]:
    if tty and os.name != "nt":
        import pty

        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            text=False,
        )
        return process, master_fd, slave_fd
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    process = subprocess.Popen(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        text=False,
    )
    return process, None, None
