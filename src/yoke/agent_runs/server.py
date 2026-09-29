"""Lazy local reporting server, authenticated by per-launch capabilities."""

from __future__ import annotations

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
            with connection:
                deadline = time.monotonic() + OPERATION_TIMEOUT
                try:
                    frame = receive(connection, deadline)
                    if self.peer_credentials:
                        pid, uid, _ = struct.unpack(
                            "3i",
                            connection.getsockopt(
                                socket.SOL_SOCKET, socket.SO_PEERCRED, 12
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
                    try:
                        send(connection, {"ok": False}, deadline)
                    except Exception:
                        pass

    def close(self) -> None:
        self.stopped.set()
        self.socket.close()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=OPERATION_TIMEOUT + 0.3)
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
