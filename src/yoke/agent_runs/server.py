"""Lazy local reporting server, authenticated by per-launch capabilities."""

from __future__ import annotations

from contextlib import suppress
import logging

import os
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from yoke.agent_runs.protocol import OPERATION_TIMEOUT, receive, send

if TYPE_CHECKING:
    from yoke.agent_runs.registry import AgentRunRegistry


logger = logging.getLogger(__name__)


class Server:
    def __init__(self, registry: AgentRunRegistry) -> None:
        self.registry = registry
        self.peer_credentials = hasattr(socket, "SO_PEERCRED")
        self.directory: str | None = None
        if self.peer_credentials:
            self.directory = tempfile.mkdtemp(prefix="yoke-runs-")
            os.chmod(self.directory, 0o700)
            self.address = str(Path(self.directory) / "host.sock")
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            # A loopback-only fallback for hosts without Unix peer credentials.
            # The capability still fixes ownership; the reported PID is read-only.
            self.address = ""
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.stopped = threading.Event()
        try:
            if self.peer_credentials:
                self.socket.bind(self.address)
                os.chmod(self.address, 0o600)
            else:
                self.socket.bind(("127.0.0.1", 0))
                self.address = f"tcp:{self.socket.getsockname()[1]}"
            self.socket.listen(64)
            self.socket.settimeout(0.2)
            self.thread = threading.Thread(
                target=self._serve, name="yoke-agent-run-host", daemon=True
            )
            self.thread.start()
        except BaseException:
            self.socket.close()
            if self.directory is not None:
                shutil.rmtree(self.directory)
            raise

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if self.stopped.is_set():
                connection.close()
                return
            with connection:
                deadline = time.monotonic() + OPERATION_TIMEOUT
                try:
                    frame = receive(connection, deadline)
                    if self.peer_credentials:
                        pid, uid, _ = struct.unpack(
                            "3i",
                            connection.getsockopt(
                                socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED"), 12
                            ),
                        )
                        if uid != os.getuid():
                            continue
                    else:
                        pid = frame.get("pid")
                        if type(pid) is not int or pid <= 0:
                            continue
                    result = self.registry.dispatch(frame, pid=pid)
                    send(connection, {"ok": True, **result}, deadline)
                except Exception:
                    logger.warning("Agent run dispatch failed", exc_info=True)
                    try:
                        send(connection, {"ok": False}, deadline)
                    except Exception:
                        logger.debug(
                            "Could not send agent run failure response", exc_info=True
                        )

    def _wake_accept(self) -> None:
        """Connect once so a listener blocked in accept() returns promptly."""
        try:
            with socket.socket(self.socket.family, socket.SOCK_STREAM) as wake:
                wake.settimeout(OPERATION_TIMEOUT)
                if self.socket.family == getattr(socket, "AF_UNIX", None):
                    wake.connect(self.address)
                else:
                    wake.connect(("127.0.0.1", self.socket.getsockname()[1]))
        except OSError:
            pass

    def close(self) -> None:
        self.stopped.set()
        # close() alone need not interrupt another thread blocked in accept(),
        # and macOS does not wake a timed accept when the descriptor closes. A
        # throwaway connection lets the loop observe the stop flag on every OS.
        self._wake_accept()
        with suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=OPERATION_TIMEOUT + 0.3)
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
