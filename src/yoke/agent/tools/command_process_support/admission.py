"""Serialize process admission without holding the reader notification lock."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_support.spawn import open_process
from yoke.agent.tools.command_process_types import MAX_PROCESS_COUNT
from yoke.agent.tools.python_env import prepare_python_env
from yoke.agent.tools.shell import build_shell_command

if TYPE_CHECKING:
    from yoke.agent.tools.command_process_manager import CommandProcessManager


def spawn(
    manager: CommandProcessManager,
    command: str,
    cwd: Path,
    tty: bool,
    shell: str | None,
    login: bool,
    *,
    argv: list[str] | None,
    env: dict[str, str] | None,
    timeout_seconds: int | None,
) -> _ManagedCommandProcess:
    with manager._spawn_lock:
        _prune(manager)
        managed: _ManagedCommandProcess | None = None
        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            with manager._lock:
                if manager._closed:
                    raise RuntimeError("Command process manager is closed")
                session_id = manager._allocate_session_id()
                process_env = (
                    env.copy() if env is not None else manager.base_environment()
                )
                if env is None:
                    prepare_python_env(process_env)
                process_argv = argv or build_shell_command(
                    command, process_env, shell=shell, login=login
                )
                process, master_fd, slave_fd = open_process(
                    process_argv, cwd, process_env, tty=tty
                )
                managed = manager._managed_process_factory(
                    session_id=session_id,
                    command=command,
                    cwd=cwd,
                    process=process,
                    tty=tty,
                    master_fd=master_fd,
                    on_change=manager._notify,
                    timeout_seconds=timeout_seconds,
                )
                manager._processes[session_id] = managed
                managed.start_readers()
        except BaseException:
            if managed is not None:
                try:
                    managed.terminate()
                except BaseException:
                    # Keep failed cleanup owned by the manager for a later retry.
                    pass
                else:
                    with manager._lock:
                        manager._processes.pop(managed.session_id, None)
            elif master_fd is not None:
                os.close(master_fd)
            raise
        finally:
            if slave_fd is not None:
                os.close(slave_fd)
    manager._notify()
    return managed


def _prune(manager: CommandProcessManager) -> None:
    with manager._lock:
        if manager._closed:
            raise RuntimeError("Command process manager is closed")
        if len(manager._processes) < MAX_PROCESS_COUNT:
            return
        oldest = min(
            manager._processes.values(),
            key=lambda process: (not process.finished, process.last_used_at),
        )
    if oldest.finished:
        manager._complete(oldest.session_id)
    else:
        oldest.terminate()
        with manager._lock:
            if manager._processes.get(oldest.session_id) is oldest:
                manager._processes.pop(oldest.session_id)
                manager._background_session_ids.discard(oldest.session_id)
                manager._notified_completion_ids.discard(oldest.session_id)
