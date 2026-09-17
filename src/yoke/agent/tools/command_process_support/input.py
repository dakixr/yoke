"""Bounded, cancellable stdin writes with explicit partial-delivery outcomes."""

from __future__ import annotations

from contextlib import suppress
import os
import time

from yoke.agent.tools.command_process_types import CancelRequested


MAX_STDIN_DELIVERY_SECONDS = 5.0
WRITE_CHUNK_BYTES = 4096


class StdinDeliveryError(RuntimeError):
    """Stdin delivery stopped, with the known count accepted by the OS."""

    def __init__(self, message: str, *, written: int, cancelled: bool = False) -> None:
        super().__init__(message)
        self.written = written
        self.cancelled = cancelled


def write_all(fd: int, raw: bytes, *, cancel_requested: CancelRequested | None) -> int:
    """Write to an owned duplicate descriptor and close it on every exit path."""
    written = 0
    blocking: bool | None = None
    deadline = time.monotonic() + MAX_STDIN_DELIVERY_SECONDS
    try:
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        view = memoryview(raw)
        while written < len(raw):
            if cancel_requested is not None and cancel_requested():
                raise StdinDeliveryError(
                    "Stdin delivery cancelled; inspect input_bytes_written before retrying",
                    written=written,
                    cancelled=True,
                )
            if time.monotonic() >= deadline:
                raise StdinDeliveryError(
                    "Stdin delivery exceeded its 5-second safety limit; the process is not reading fast enough",
                    written=written,
                )
            try:
                count = os.write(fd, view[written : written + WRITE_CHUNK_BYTES])
            except BlockingIOError:
                count = 0
            except InterruptedError:
                continue
            if count:
                written += count
            else:
                time.sleep(0.01)
        return written
    except OSError as exc:
        raise StdinDeliveryError(str(exc), written=written) from exc
    finally:
        # dup() shares descriptor flags with the original, but keeps this exact
        # open file alive even if runtime cleanup closes/reuses the original fd.
        if blocking is not None:
            with suppress(OSError):
                os.set_blocking(fd, blocking)
        os.close(fd)
