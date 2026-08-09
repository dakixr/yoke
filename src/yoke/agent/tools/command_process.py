"""Managed background command processes shared across agent turns."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from yoke.agent.tools.command_process_types import INTERRUPT
from yoke.agent.tools.command_process_types import (
    MAX_RETAINED_OUTPUT_BYTES,
)
from yoke.agent.tools.command_process_types import CancelRequested
from yoke.agent.tools.command_process_types import CommandProcessResult
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.agent.tools.command_process_types import (
    decode_command_output_chunk,
)


class _ManagedCommandProcess:
    """Own one child process plus buffered output readers."""

    def __init__(
        self,
        *,
        session_id: int,
        command: str,
        cwd: Path,
        process: subprocess.Popen[bytes],
        tty: bool,
        master_fd: int | None,
        on_change: Callable[[], None],
        timeout_seconds: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.command = command
        self.cwd = cwd
        self.process = process
        self.tty = tty
        self.master_fd = master_fd
        self.on_change = on_change
        self.timeout_seconds = timeout_seconds
        self.timed_out = False
        self.started_at = datetime.now().astimezone()
        self.started_monotonic = time.monotonic()
        self.finished_monotonic: float | None = None
        self.last_used_at = self.started_monotonic
        self.condition = threading.Condition()
        self.pending: deque[bytes] = deque()
        self.pending_bytes = 0
        self.pending_original_bytes = 0
        self.history: deque[bytes] = deque()
        self.history_bytes = 0
        self.original_output_bytes = 0
        self.open_readers = 0
        self.closed = False

    def start_readers(self) -> None:
        if self.master_fd is not None:
            self.open_readers = 1
            self._start_reader(lambda: os.read(self.master_fd or -1, 4096))
        else:
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is None:
                    continue
                self.open_readers += 1
                self._start_reader(lambda pipe=pipe: os.read(pipe.fileno(), 4096))
        threading.Thread(
            target=self._watch_exit,
            daemon=True,
            name=f"yoke-command-{self.session_id}-watcher",
        ).start()
        if self.timeout_seconds is not None:
            threading.Thread(
                target=self._watch_timeout,
                daemon=True,
                name=f"yoke-command-{self.session_id}-timeout",
            ).start()

    def _start_reader(self, read_chunk: Callable[[], bytes]) -> None:
        threading.Thread(
            target=self._reader_main,
            args=(read_chunk,),
            daemon=True,
            name=f"yoke-command-{self.session_id}-reader",
        ).start()

    def _reader_main(self, read_chunk: Callable[[], bytes]) -> None:
        try:
            while raw := read_chunk():
                self._append_output(raw)
        except OSError:
            pass
        finally:
            with self.condition:
                self.open_readers = max(0, self.open_readers - 1)
                self.condition.notify_all()
            self.on_change()

    def _append_output(self, raw: bytes) -> None:
        with self.condition:
            self.pending.append(raw)
            self.pending_bytes += len(raw)
            self.pending_original_bytes += len(raw)
            self.history.append(raw)
            self.history_bytes += len(raw)
            self.original_output_bytes += len(raw)
            while self.pending_bytes > MAX_RETAINED_OUTPUT_BYTES:
                dropped = self.pending.popleft()
                self.pending_bytes -= len(dropped)
            while self.history_bytes > MAX_RETAINED_OUTPUT_BYTES:
                dropped = self.history.popleft()
                self.history_bytes -= len(dropped)
            self.condition.notify_all()
        self.on_change()

    def _watch_exit(self) -> None:
        self.process.wait()
        with self.condition:
            while self.open_readers:
                self.condition.wait(timeout=0.05)
            self.finished_monotonic = time.monotonic()
            self.condition.notify_all()
        self.on_change()

    def _watch_timeout(self) -> None:
        try:
            self.process.wait(timeout=self.timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        with self.condition:
            if self.process.poll() is not None:
                return
            self.timed_out = True
        self._terminate_running_process()
        self.on_change()

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
        output = "".join(decode_command_output_chunk(raw) for raw in chunks)
        return CommandProcessResult(
            session_id=None if finished else self.session_id,
            exit_code=exit_code,
            output=output.replace("\r\n", "\n").replace("\r", "\n"),
            wall_time_seconds=time.monotonic() - started_at,
            original_output_bytes=original_output_bytes,
            timed_out=self.timed_out,
        )

    def snapshot(self) -> CommandProcessSnapshot:
        """Return current process metadata without consuming buffered output."""
        with self.condition:
            finished = self.finished
            output = "".join(decode_command_output_chunk(raw) for raw in self.history)
            return CommandProcessSnapshot(
                session_id=self.session_id,
                pid=self.process.pid,
                command=self.command,
                cwd=self.cwd,
                tty=self.tty,
                status=self._status(finished),
                started_at=self.started_at,
                elapsed_seconds=max(
                    0.0,
                    (self.finished_monotonic or time.monotonic())
                    - self.started_monotonic,
                ),
                exit_code=self.process.poll() if finished else None,
                output_tail=output.replace("\r\n", "\n").replace("\r", "\n"),
                original_output_bytes=self.original_output_bytes,
                retained_output_bytes=self.history_bytes,
            )

    def _status(self, finished: bool) -> Literal["running", "exited", "failed"]:
        if not finished:
            return "running"
        return "exited" if self.process.poll() == 0 else "failed"

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
            raise RuntimeError("stdin is closed for this command session")
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def interrupt(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name != "nt":
            os.killpg(self.process.pid, signal.SIGINT)
            return
        self.process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))

    def terminate(self) -> None:
        if self.process.poll() is None:
            self._terminate_running_process()
        self.close()

    def _terminate_running_process(self) -> None:
        if os.name == "nt":
            taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
            try:
                subprocess.run(  # noqa: S603
                    [
                        taskkill or "taskkill.exe",
                        "/PID",
                        str(self.process.pid),
                        "/T",
                        "/F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        pipes = (self.process.stdin, self.process.stdout, self.process.stderr)
        for pipe in pipes:
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
