"""Read-only process death checks, bound to a kernel handle or birth identity."""

from __future__ import annotations

import os
from pathlib import Path
import select
import sys


def _proc_identity(pid: int) -> tuple[str, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19], fields[0]
    except (FileNotFoundError, ProcessLookupError):
        return None


class Worker:
    """Never signal a reported PID. Retain identity through PID reuse."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.fd: int | None = None
        self.birth: str | None = None
        self._queue = None
        self._handle = None
        self._kernel = None

    @classmethod
    def open(cls, pid: int) -> Worker:
        worker = cls(pid)
        if hasattr(os, "pidfd_open"):
            try:
                worker.fd = os.pidfd_open(pid)
                return worker
            except OSError:
                pass
        if sys.platform.startswith("linux"):
            identity = _proc_identity(pid)
            if identity is None or identity[1] in {"Z", "X"}:
                raise ProcessLookupError("Agent worker exited")
            worker.birth = identity[0]
        elif sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
            if not handle:
                raise OSError("Cannot observe agent worker")
            worker._kernel = kernel
            worker._handle = handle
        elif sys.platform == "darwin" or sys.platform.startswith(
            ("freebsd", "openbsd", "netbsd")
        ):
            queue = getattr(select, "kqueue")()
            try:
                queue.control(
                    [
                        getattr(select, "kevent")(
                            pid,
                            filter=getattr(select, "KQ_FILTER_PROC"),
                            flags=getattr(select, "KQ_EV_ADD")
                            | getattr(select, "KQ_EV_ONESHOT"),
                            fflags=getattr(select, "KQ_NOTE_EXIT"),
                        )
                    ],
                    0,
                    0,
                )
            except BaseException:
                queue.close()
                raise
            worker._queue = queue
        else:
            raise RuntimeError("This platform cannot observe agent worker death")
        return worker

    def dead(self) -> bool:
        if self.fd is not None:
            poller = select.poll()
            poller.register(self.fd, select.POLLIN)
            return bool(poller.poll(0))
        if self.birth is not None:
            identity = _proc_identity(self.pid)
            return (
                identity is None
                or identity[0] != self.birth
                or identity[1] in {"Z", "X"}
            )
        if self._queue is not None:
            return bool(self._queue.control([], 1, 0))
        if self._kernel is not None and self._handle is not None:
            return self._kernel.WaitForSingleObject(self._handle, 0) == 0
        return False

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.birth = None
        if self._queue is not None:
            self._queue.close()
            self._queue = None
        if self._kernel is not None and self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
