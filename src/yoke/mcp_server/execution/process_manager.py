"""MCP-specific incremental UTF-8 decoding, without changing agent output."""

from __future__ import annotations

import codecs
import os
from collections.abc import Callable
from pathlib import Path

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
    _open_process,
)
from yoke.agent.tools.python_env import prepare_python_env
from yoke.agent.tools.shell import build_shell_command


class UTF8CommandProcess(_ManagedCommandProcess):
    def _reader_main(self, read_chunk: Callable[[], bytes]) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while raw := read_chunk():
                decoded = decoder.decode(raw)
                if decoded:
                    self._append_output(decoded.encode("utf-8"))
        except OSError:
            pass
        finally:
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append_output(tail.encode("utf-8"))
            with self.condition:
                self.open_readers = max(0, self.open_readers - 1)
                self.condition.notify_all()
            self.on_change()


class MCPProcessManager(CommandProcessManager):
    """Use the normal process lifecycle with a reader that retains whole characters."""

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
            process_env = env.copy() if env is not None else self.base_environment()
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
                managed = UTF8CommandProcess(
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
