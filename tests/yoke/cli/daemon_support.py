"""An actual Unix HTTP listener around the application and fixture runtime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import uvicorn
from fastapi import FastAPI

from tests.yoke.fixture_server import FixtureServer
from yoke.daemon.client import DaemonHandle


@contextmanager
def serve_app(app: FastAPI) -> Iterator[DaemonHandle]:
    """Serve only on a temporary socket; close all streams before shutdown."""
    with TemporaryDirectory(prefix="yk-cli-") as directory:
        socket = Path(directory) / "http.sock"
        server = FixtureServer(
            uvicorn.Config(app, uds=str(socket), log_level="error", ws="none")
        )
        worker = Thread(target=server.run, daemon=True)
        worker.start()
        try:
            if not server.ready.wait(5) or not worker.is_alive():
                raise AssertionError("In-test daemon did not start")
            if server.error is not None:
                raise server.error
            yield DaemonHandle(socket, 0, "fixture", "1")
        finally:
            app.state.event_broker.close()
            server.stop()
            worker.join(timeout=10)
            assert not worker.is_alive(), "In-test daemon survived shutdown"
            if server.error is not None:
                raise server.error
