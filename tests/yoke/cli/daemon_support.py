"""An actual Unix HTTP listener around the application and fixture runtime."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import uvicorn
from fastapi import FastAPI

from yoke.daemon.client import DaemonHandle


@contextmanager
def serve_app(app: FastAPI) -> Iterator[DaemonHandle]:
    """Serve only on a temporary socket; close all streams before shutdown."""
    with TemporaryDirectory(prefix="yk-cli-") as directory:
        socket = Path(directory) / "http.sock"
        server = uvicorn.Server(
            uvicorn.Config(app, uds=str(socket), log_level="error", ws="none")
        )
        worker = Thread(target=server.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if not worker.is_alive() or time.monotonic() >= deadline:
                    raise AssertionError("In-test daemon did not start")
                time.sleep(0.01)
            yield DaemonHandle(socket, 0, "fixture", "1")
        finally:
            app.state.event_broker.close()
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive(), "In-test daemon survived shutdown"
