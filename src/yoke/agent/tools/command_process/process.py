"""Managed background command processes shared across agent turns."""

from __future__ import annotations

import codecs
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from yoke.agent.tools.command_process.coordination import acquired
from yoke.agent.tools.command_process.input import write_all
from yoke.agent.tools.command_process.lifecycle import (
    terminate_owned_process_tree,
)
from yoke.agent.tools.command_process.output import (
    CompletedCommandProcess,
    RetainedProcessOutput,
    command_decoder,
)
from yoke.agent.tools.command_process.phase import (
    TERMINAL as TERMINAL_PHASES,
    ProcessEvent,
    ProcessPhase,
    next_process_phase,
    process_status,
)
from yoke.agent.tools.command_process.types import (
    INTERRUPT,
    CancelRequested,
    CommandProcessOutputPage,
    CommandProcessResult,
    CommandProcessSnapshot,
)

FINAL_DRAIN_SECONDS = 1.0
READER_JOIN_RESERVE_SECONDS = 0.1
LOGGER = logging.getLogger(__name__)


class _ManagedCommandProcess:
    """Own one child process plus buffered output readers."""

    decoder_factory: Callable[[], codecs.IncrementalDecoder] = staticmethod(
        command_decoder
    )

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
        self.input_lock = threading.RLock()
        self.output = RetainedProcessOutput()
        self.open_readers = 0
        self._reader_threads: list[threading.Thread] = []
        self._watcher_thread: threading.Thread | None = None
        self._timeout_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._process_group_id = process.pid
        self._accepting_output = True
        # Local descriptor release is orthogonal to the lifecycle: an EXITED or
        # CLEANUP_FAILED process can still hold local resources, and releasing
        # them does not by itself finish the lifecycle. It stays plain data.
        self._local_resources_released = False
        self._phase = ProcessPhase.STARTING

    def start_readers(self) -> None:
        if self._phase is ProcessPhase.STARTING:
            self._enter(ProcessEvent.START)
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
        decoder = self.decoder_factory()
        try:
            for raw in self._read_chunks(read_chunk):
                decoded = decoder.decode(raw)
                if decoded:
                    self._append_output(decoded.encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append_output(tail.encode("utf-8"))
            self._reader_finished()

    def _read_chunks(self, read_chunk: Callable[[], bytes]) -> Iterator[bytes]:
        while self._accepting_output:
            try:
                raw = read_chunk()
            except BlockingIOError:
                # A bounded PTY write temporarily makes the shared master
                # descriptor nonblocking. This is not EOF.
                with self.condition:
                    self.condition.wait(timeout=0.01)
                continue
            if not raw:
                return
            yield raw

    def _reader_finished(self) -> None:
        with self.condition:
            self.open_readers = max(0, self.open_readers - 1)
            self._observe_exit_locked()
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
            self._observe_exit_locked()
            self.condition.notify_all()
        self.on_change()

    def _watch_timeout(self) -> None:
        assert self.timeout_seconds is not None
        deadline = self.started_monotonic + self.timeout_seconds
        with self.condition:
            while not self._finished_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 0.05))
            if self._finished_locked():
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
            while not self._finished_locked():
                if cancel_requested is not None and cancel_requested():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 0.05))
            output, original_output_bytes = self.output.consume()
            self.last_used_at = time.monotonic()
            finished = self._finished_locked()
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

    def _snapshot_locked(
        self, *, include_output: bool = True
    ) -> CommandProcessSnapshot:
        self._observe_exit_locked()
        phase = self._phase
        exit_code = self.process.poll()
        return CommandProcessSnapshot(
            session_id=self.session_id,
            pid=self.process.pid,
            command=self.command,
            cwd=self.cwd,
            tty=self.tty,
            status=process_status(phase, exit_code),
            started_at=self.started_at,
            elapsed_seconds=max(
                0.0,
                (self.finished_monotonic or time.monotonic()) - self.started_monotonic,
            ),
            exit_code=exit_code
            if phase in {ProcessPhase.EXITED, ProcessPhase.CLEANUP_FAILED}
            else None,
            output_tail=self.output.tail() if include_output else "",
            original_output_bytes=self.output.original_bytes,
            retained_output_bytes=self.output.retained_bytes,
            latest_output_seq=self.output.latest_seq,
            truncated_before_seq=self.output.truncated_before_seq,
            timed_out=self.timed_out,
        )

    def output_page(
        self, *, after_seq: int, limit: int, exact: bool = False
    ) -> CommandProcessOutputPage:
        """Return retained output chunks after an exclusive sequence cursor."""
        with self.condition:
            return self.output.page(after_seq=after_seq, limit=limit, exact=exact)

    @property
    def phase(self) -> ProcessPhase:
        """Return the owned lifecycle phase."""
        with self.condition:
            self._observe_exit_locked()
            return self._phase

    def _finished_locked(self) -> bool:
        self._observe_exit_locked()
        return self._phase in TERMINAL_PHASES

    @property
    def finished(self) -> bool:
        with self.condition:
            return self._finished_locked()

    @property
    def closed(self) -> bool:
        """Whether local resources are released; tracked outside the phase."""
        with self.condition:
            return self._local_resources_released

    def _enter(self, event: ProcessEvent) -> ProcessPhase:
        with self.condition:
            return self._enter_locked(event)

    def _enter_locked(self, event: ProcessEvent) -> ProcessPhase:
        previous = self._phase
        current = next_process_phase(previous, event)
        if current is not previous:
            if current is ProcessPhase.EXITED and self.finished_monotonic is None:
                self.finished_monotonic = time.monotonic()
            LOGGER.debug(
                "machine=command_process id=%s from=%s to=%s cause=%s",
                self.session_id,
                previous.value,
                current.value,
                event.value,
            )
            self._phase = current
            self.condition.notify_all()
        return current

    def write(
        self, chars: str, *, cancel_requested: CancelRequested | None = None
    ) -> int:
        raw = chars.encode("utf-8")
        with acquired(self.input_lock, cancel_requested):
            if chars == INTERRUPT and not self.tty:
                self.interrupt()
                return 0
            with acquired(self._lifecycle_lock, cancel_requested):
                if self.master_fd is not None:
                    fd = os.dup(self.master_fd)
                elif self.process.stdin is not None:
                    fd = os.dup(self.process.stdin.fileno())
                else:
                    raise RuntimeError("stdin is closed for this command session")
            return write_all(fd, raw, cancel_requested=cancel_requested)

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
                self._enter_locked(
                    ProcessEvent.RETRY
                    if self._phase is ProcessPhase.CLEANUP_FAILED
                    else ProcessEvent.SHUTDOWN
                )
                already_exited = self._phase is ProcessPhase.EXITED
            if not already_exited:
                try:
                    terminate_owned_process_tree(
                        self.process,
                        self._process_group_id,
                    )
                except BaseException as exc:
                    first_error = exc
            try:
                self._close_local_resources()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            with self.condition:
                self._enter_locked(
                    ProcessEvent.CLEANUP_FAILED
                    if first_error is not None
                    else ProcessEvent.CLEANUP_COMPLETE
                )
        if first_error is not None:
            raise first_error

    def _close_local_resources(self) -> None:
        if self._local_resources_released:
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
            self._observe_exit_locked()
            self.condition.notify_all()
        if live_readers and first_error is None:
            first_error = RuntimeError(
                f"Timed out draining command session {self.session_id} output"
            )
        if first_error is None:
            self._local_resources_released = True
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

    def _observe_exit_locked(self) -> None:
        """Record a reaped child once every reader has drained.

        Idempotent. Reader and watcher threads call it when they see the child
        go, and read paths call it so a reaped child is never missed.
        """
        if (
            self._phase in {ProcessPhase.STARTING, ProcessPhase.RUNNING}
            and self.open_readers == 0
            and self.process.poll() is not None
        ):
            self._enter_locked(ProcessEvent.EXIT)
