"""Managed background command processes shared across agent turns."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from yoke.agent.tools.command_process_support.lifecycle import (
    terminate_owned_process_tree,
)
from yoke.agent.tools.command_process_support.output import CompletedCommandProcess
from yoke.agent.tools.command_process_support.output import RetainedProcessOutput
from yoke.agent.tools.command_process_types import INTERRUPT
from yoke.agent.tools.command_process_types import CancelRequested
from yoke.agent.tools.command_process_types import CommandProcessResult
from yoke.agent.tools.command_process_types import (
    CommandProcessOutputPage,
)
from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)

FINAL_DRAIN_SECONDS = 1.0
READER_JOIN_RESERVE_SECONDS = 0.1


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
        self.output = RetainedProcessOutput()
        self.open_readers = 0
        self._reader_threads: list[threading.Thread] = []
        self._watcher_thread: threading.Thread | None = None
        self._timeout_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._process_group_id = process.pid
        self._accepting_output = True
        self._tree_cleanup_complete = False
        self._local_cleanup_complete = False
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
        self._watcher_thread = threading.Thread(
            target=self._watch_exit,
            daemon=True,
            name=f"yoke-command-{self.session_id}-watcher",
        )
        self._watcher_thread.start()
        if self.timeout_seconds is not None:
            self._timeout_thread = threading.Thread(
                target=self._watch_timeout,
                daemon=True,
                name=f"yoke-command-{self.session_id}-timeout",
            )
            self._timeout_thread.start()

    def _start_reader(self, read_chunk: Callable[[], bytes]) -> None:
        thread = threading.Thread(
            target=self._reader_main,
            args=(read_chunk,),
            daemon=True,
            name=f"yoke-command-{self.session_id}-reader",
        )
        self._reader_threads.append(thread)
        thread.start()

    def _reader_main(self, read_chunk: Callable[[], bytes]) -> None:
        try:
            while raw := read_chunk():
                self._append_output(raw)
        except OSError:
            pass
        finally:
            self._reader_finished()

    def _reader_finished(self) -> None:
        with self.condition:
            self.open_readers = max(0, self.open_readers - 1)
            self._mark_finished_locked()
            self.condition.notify_all()
        self.on_change()

    def _append_output(self, raw: bytes) -> None:
        with self.condition:
            if not self._accepting_output:
                return
            self.output.append(raw)
            self.condition.notify_all()
        self.on_change()

    def _watch_exit(self) -> None:
        self.process.wait()
        with self.condition:
            while self.open_readers:
                self.condition.wait(timeout=0.05)
            self._mark_finished_locked()
            self.condition.notify_all()
        self.on_change()

    def _watch_timeout(self) -> None:
        assert self.timeout_seconds is not None
        deadline = self.started_monotonic + self.timeout_seconds
        with self.condition:
            while not self.finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 0.05))
            if self.finished:
                return
            self.timed_out = True
        try:
            self.terminate()
        finally:
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
            output, original_output_bytes = self.output.consume()
            self.last_used_at = time.monotonic()
            finished = self.finished
            exit_code = self.process.poll() if finished else None
        return CommandProcessResult(
            session_id=None if finished else self.session_id,
            exit_code=exit_code,
            output=output,
            wall_time_seconds=time.monotonic() - started_at,
            original_output_bytes=original_output_bytes,
            timed_out=self.timed_out,
        )

    def snapshot(self) -> CommandProcessSnapshot:
        """Return current process metadata without consuming buffered output."""
        with self.condition:
            return self._snapshot_locked()

    def completed_record(self) -> CompletedCommandProcess:
        """Freeze one snapshot and its matching retained output sequence ring."""
        with self.condition:
            return CompletedCommandProcess(
                snapshot=self._snapshot_locked(),
                output_records=self.output.freeze(),
            )

    def _snapshot_locked(self) -> CommandProcessSnapshot:
        finished = self.finished
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
                (self.finished_monotonic or time.monotonic()) - self.started_monotonic,
            ),
            exit_code=self.process.poll() if finished else None,
            output_tail=self.output.tail(),
            original_output_bytes=self.output.original_bytes,
            retained_output_bytes=self.output.retained_bytes,
            latest_output_seq=self.output.latest_seq,
            truncated_before_seq=self.output.truncated_before_seq,
        )

    def output_page(self, *, after_seq: int, limit: int) -> CommandProcessOutputPage:
        """Return retained output chunks after an exclusive sequence cursor."""
        with self.condition:
            return self.output.page(after_seq=after_seq, limit=limit)

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
            os.killpg(self._process_group_id, signal.SIGINT)
            return
        self.process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))

    def terminate(self) -> None:
        """Terminate the owned tree, then bound pipe drain and local cleanup."""
        self._shutdown()

    def close(self) -> None:
        """Release the process tree and every local descriptor."""
        self._shutdown()

    def _shutdown(self) -> None:
        first_error: BaseException | None = None
        with self._lifecycle_lock:
            with self.condition:
                if self.finished:
                    self._tree_cleanup_complete = True
            if not self._tree_cleanup_complete:
                try:
                    terminate_owned_process_tree(
                        self.process,
                        self._process_group_id,
                    )
                except BaseException as exc:
                    first_error = exc
                else:
                    self._tree_cleanup_complete = True
            try:
                self._close_local_resources()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _close_local_resources(self) -> None:
        if self._local_cleanup_complete:
            return
        deadline = time.monotonic() + FINAL_DRAIN_SECONDS
        drain_deadline = deadline - READER_JOIN_RESERVE_SECONDS
        with self.condition:
            while self.open_readers and time.monotonic() < drain_deadline:
                self.condition.wait(
                    timeout=min(0.05, drain_deadline - time.monotonic())
                )

        first_error = self._close_descriptors()
        for reader in self._reader_threads:
            if reader is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            reader.join(timeout=remaining)

        live_readers = [reader for reader in self._reader_threads if reader.is_alive()]
        with self.condition:
            self._accepting_output = False
            if live_readers:
                self.open_readers = 0
            self._mark_finished_locked()
            self.condition.notify_all()
        if live_readers and first_error is None:
            first_error = RuntimeError(
                f"Timed out draining command session {self.session_id} output"
            )
        if first_error is None:
            self._local_cleanup_complete = True
            self.closed = True
        if first_error is not None:
            raise first_error

    def _close_descriptors(self) -> BaseException | None:
        first_error: BaseException | None = None
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except OSError:
                pass
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        return first_error

    def _mark_finished_locked(self) -> None:
        if (
            self.finished_monotonic is None
            and self.process.poll() is not None
            and self.open_readers == 0
        ):
            self.finished_monotonic = time.monotonic()
