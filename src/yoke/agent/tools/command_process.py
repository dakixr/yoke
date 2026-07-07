"""Managed background command processes shared across agent turns."""

from __future__ import annotations

import atexit
import os
import random
import signal
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yoke.agent.tools.python.env import prepare_python_env
from yoke.agent.tools.shell import build_shell_command

MIN_YIELD_TIME_MS = 250
MAX_YIELD_TIME_MS = 7_200_000
DEFAULT_EXEC_YIELD_TIME_MS = 30_000
DEFAULT_WRITE_YIELD_TIME_MS = 30_000
DEFAULT_POLL_YIELD_TIME_MS = 30_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
MAX_PROCESS_COUNT = 64
MAX_RETAINED_OUTPUT_BYTES = 1024 * 1024
INTERRUPT = "\x03"

ToolEvent = Callable[[str, dict[str, object]], None]
CancelRequested = Callable[[], bool]


def decode_command_output_chunk(raw: bytes) -> str:
    """Decode one streamed command-output chunk."""
    if not raw:
        return ""
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass(slots=True, frozen=True)
class BackgroundProcessInfo:
    """User-visible information about one running command."""

    session_id: int
    command: str
    cwd: Path
    started_at: float
    tty: bool


@dataclass(slots=True, frozen=True)
class CommandProcessResult:
    """Output observed during one exec or stdin interaction."""

    session_id: int | None
    exit_code: int | None
    output: str
    wall_time_seconds: float
    original_output_bytes: int


class _ManagedProcess:
    def __init__(
        self,
        *,
        session_id: int,
        command: str,
        cwd: Path,
        process: subprocess.Popen[bytes],
        tty: bool,
        master_fd: int | None,
        tool_event: ToolEvent | None,
    ) -> None:
        self.session_id = session_id
        self.command = command
        self.cwd = cwd
        self.process = process
        self.tty = tty
        self.master_fd = master_fd
        self.tool_event = tool_event
        self.started_at = time.monotonic()
        self.last_used_at = self.started_at
        self.condition = threading.Condition()
        self.pending: deque[tuple[str, bytes]] = deque()
        self.pending_bytes = 0
        self.pending_original_bytes = 0
        self.open_readers = 0
        self.closed = False
        self._reader_threads: list[threading.Thread] = []
        self._watcher_thread: threading.Thread | None = None

    def start_readers(self) -> None:
        if self.master_fd is not None:
            self.open_readers = 1
            self._start_reader("stdout", self._read_pty)
        else:
            for stream, pipe in (
                ("stdout", self.process.stdout),
                ("stderr", self.process.stderr),
            ):
                if pipe is None:
                    continue
                self.open_readers += 1
                self._start_reader(stream, lambda pipe=pipe: self._read_pipe(pipe))
        self._watcher_thread = threading.Thread(
            target=self._watch_exit,
            daemon=True,
            name=f"yoke-command-{self.session_id}-watcher",
        )
        self._watcher_thread.start()

    def _start_reader(
        self,
        stream: str,
        read_chunks: Callable[[], bytes],
    ) -> None:
        thread = threading.Thread(
            target=self._reader_main,
            args=(stream, read_chunks),
            daemon=True,
            name=f"yoke-command-{self.session_id}-{stream}",
        )
        self._reader_threads.append(thread)
        thread.start()

    def _reader_main(
        self,
        stream: str,
        read_chunks: Callable[[], bytes],
    ) -> None:
        try:
            while raw := read_chunks():
                self._append_output(stream, raw)
        except OSError:
            pass
        finally:
            with self.condition:
                self.open_readers = max(0, self.open_readers - 1)
                self.condition.notify_all()

    def _read_pipe(self, pipe: Any) -> bytes:
        return os.read(pipe.fileno(), 4096)

    def _read_pty(self) -> bytes:
        if self.master_fd is None:
            return b""
        return os.read(self.master_fd, 4096)

    def _append_output(self, stream: str, raw: bytes) -> None:
        with self.condition:
            self.pending.append((stream, raw))
            self.pending_bytes += len(raw)
            self.pending_original_bytes += len(raw)
            while self.pending_bytes > MAX_RETAINED_OUTPUT_BYTES and self.pending:
                _, dropped = self.pending.popleft()
                self.pending_bytes -= len(dropped)
            self.condition.notify_all()
        if self.tool_event is not None:
            try:
                self.tool_event(
                    "tool_execution_output_delta",
                    {
                        "stream": stream,
                        "text": decode_command_output_chunk(raw),
                        "session_id": self.session_id,
                    },
                )
            except Exception:
                pass

    def _watch_exit(self) -> None:
        exit_code = self.process.wait()
        with self.condition:
            while self.open_readers:
                self.condition.wait(timeout=0.05)
            self.condition.notify_all()
        if self.tool_event is not None:
            try:
                self.tool_event(
                    "background_process_end",
                    {
                        "session_id": self.session_id,
                        "exit_code": exit_code,
                        "elapsed_seconds": time.monotonic() - self.started_at,
                    },
                )
            except Exception:
                pass

    def wait_and_consume(
        self,
        yield_time_ms: int,
        *,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        started_at = time.monotonic()
        deadline = started_at + yield_time_ms / 1000
        with self.condition:
            while not self.finished:
                if cancel_requested is not None and cancel_requested():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 0.05))
            chunks = list(self.pending)
            original_output_bytes = self.pending_original_bytes
            self.pending.clear()
            self.pending_bytes = 0
            self.pending_original_bytes = 0
            self.last_used_at = time.monotonic()
            finished = self.finished
            exit_code = self.process.poll() if finished else None
        output = "".join(decode_command_output_chunk(raw) for _stream, raw in chunks)
        return CommandProcessResult(
            session_id=None if finished else self.session_id,
            exit_code=exit_code,
            output=output.replace("\r\n", "\n").replace("\r", "\n"),
            wall_time_seconds=time.monotonic() - started_at,
            original_output_bytes=original_output_bytes,
        )

    @property
    def finished(self) -> bool:
        return self.process.poll() is not None and self.open_readers == 0

    def write(self, chars: str) -> None:
        if chars == INTERRUPT and not self.tty:
            self.interrupt()
            return
        raw = chars.encode("utf-8")
        if self.master_fd is not None:
            os.write(self.master_fd, raw)
            return
        if self.process.stdin is None:
            raise RuntimeError(
                "stdin is closed for this session; rerun exec_command with tty=true"
            )
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def interrupt(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name != "nt" and self.process.pid is not None:
            os.killpg(self.process.pid, signal.SIGINT)
            return
        self.process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))

    def terminate(self) -> None:
        if self.process.poll() is None:
            if os.name != "nt" and self.process.pid is not None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if os.name != "nt" and self.process.pid is not None:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.process.kill()
                self.process.wait(timeout=1)
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


class CommandProcessManager:
    """Own running command processes for one agent runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[int, _ManagedProcess] = {}
        _ACTIVE_MANAGERS.add(self)

    def exec_command(
        self,
        *,
        command: str,
        cwd: Path,
        tty: bool,
        yield_time_ms: int,
        shell: str | None,
        login: bool,
        tool_event: ToolEvent | None,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        managed = self._spawn(
            command=command,
            cwd=cwd,
            tty=tty,
            shell=shell,
            login=login,
            tool_event=tool_event,
        )
        result = managed.wait_and_consume(
            clamp_exec_yield_time(yield_time_ms),
            cancel_requested=cancel_requested,
        )
        if result.session_id is None:
            self._remove(managed.session_id)
        return result

    def write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_ms: int | None,
        cancel_requested: CancelRequested | None,
    ) -> CommandProcessResult:
        managed = self._get(session_id)
        if chars:
            try:
                managed.write(chars)
            except (BrokenPipeError, OSError):
                if managed.process.poll() is None:
                    raise
        effective_yield = clamp_write_yield_time(yield_time_ms, has_input=bool(chars))
        result = managed.wait_and_consume(
            effective_yield,
            cancel_requested=cancel_requested,
        )
        if result.session_id is None:
            self._remove(session_id)
        return result

    def list_processes(self) -> list[BackgroundProcessInfo]:
        with self._lock:
            return [
                BackgroundProcessInfo(
                    session_id=managed.session_id,
                    command=managed.command,
                    cwd=managed.cwd,
                    started_at=managed.started_at,
                    tty=managed.tty,
                )
                for managed in sorted(
                    self._processes.values(),
                    key=lambda item: item.session_id,
                )
                if managed.process.poll() is None
            ]

    def terminate_process(self, session_id: int) -> bool:
        with self._lock:
            managed = self._processes.pop(session_id, None)
        if managed is None:
            return False
        managed.terminate()
        return True

    def terminate_all(self) -> int:
        with self._lock:
            processes = list(self._processes.values())
            self._processes.clear()
        for managed in processes:
            managed.terminate()
        return len(processes)

    def _spawn(
        self,
        *,
        command: str,
        cwd: Path,
        tty: bool,
        shell: str | None,
        login: bool,
        tool_event: ToolEvent | None,
    ) -> _ManagedProcess:
        env = os.environ.copy()
        prepare_python_env(env)
        argv = build_shell_command(
            command,
            env,
            shell=shell,
            login=login,
        )
        session_id = self._allocate_session_id()
        master_fd: int | None = None
        slave_fd: int | None = None
        try:
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
            elif os.name == "nt":
                process = subprocess.Popen(  # noqa: S603
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    text=False,
                )
            else:
                process = subprocess.Popen(  # noqa: S603
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    text=False,
                )
        except Exception:
            if master_fd is not None:
                os.close(master_fd)
            raise
        finally:
            if slave_fd is not None:
                os.close(slave_fd)
        managed = _ManagedProcess(
            session_id=session_id,
            command=command,
            cwd=cwd,
            process=process,
            tty=tty,
            master_fd=master_fd,
            tool_event=tool_event,
        )
        managed.start_readers()
        with self._lock:
            self._prune_if_needed()
            self._processes[session_id] = managed
        return managed

    def _allocate_session_id(self) -> int:
        with self._lock:
            while True:
                candidate = random.SystemRandom().randrange(1_000, 100_000)
                if candidate not in self._processes:
                    return candidate

    def _get(self, session_id: int) -> _ManagedProcess:
        with self._lock:
            managed = self._processes.get(session_id)
        if managed is None:
            raise ValueError(f"Unknown command session ID {session_id}")
        return managed

    def _remove(self, session_id: int) -> None:
        with self._lock:
            managed = self._processes.pop(session_id, None)
        if managed is not None:
            managed.close()

    def _prune_if_needed(self) -> None:
        if len(self._processes) < MAX_PROCESS_COUNT:
            return
        candidates = sorted(
            self._processes.values(),
            key=lambda item: (
                item.process.poll() is None,
                item.last_used_at,
            ),
        )
        if candidates:
            oldest = candidates[0]
            self._processes.pop(oldest.session_id, None)
            oldest.terminate()


def clamp_exec_yield_time(yield_time_ms: int) -> int:
    """Clamp an initial execution wait to supported bounds."""
    return max(MIN_YIELD_TIME_MS, min(yield_time_ms, MAX_YIELD_TIME_MS))


def clamp_write_yield_time(
    yield_time_ms: int | None,
    *,
    has_input: bool,
) -> int:
    """Resolve polling and interactive write wait bounds."""
    if yield_time_ms is None:
        return DEFAULT_WRITE_YIELD_TIME_MS if has_input else DEFAULT_POLL_YIELD_TIME_MS
    minimum = MIN_YIELD_TIME_MS if has_input else DEFAULT_POLL_YIELD_TIME_MS
    return max(minimum, min(yield_time_ms, MAX_YIELD_TIME_MS))


_ACTIVE_MANAGERS: weakref.WeakSet[CommandProcessManager] = weakref.WeakSet()


def terminate_all_command_processes() -> None:
    """Terminate managed processes when the Yoke interpreter exits."""
    for manager in list(_ACTIVE_MANAGERS):
        manager.terminate_all()


atexit.register(terminate_all_command_processes)
