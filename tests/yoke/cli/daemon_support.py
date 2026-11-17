"""An actual local HTTP listener around the application and fixture runtime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import socket
import secrets
import sys

import uvicorn
from fastapi import FastAPI

from tests.yoke.fixture_server import FixtureServer
from yoke.daemon.client import DaemonHandle
from yoke.daemon.transport import TcpEndpoint


@contextmanager
def serve_app(app: FastAPI) -> Iterator[DaemonHandle]:
    """Serve on an isolated local endpoint; close all streams before shutdown."""
    with TemporaryDirectory(prefix="yk-cli-") as directory:
        endpoint = Path(directory) / "http.sock"
        listener = None
        if sys.platform == "win32":
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            token = secrets.token_urlsafe(32)
            app.state.auth_token = token
            endpoint = TcpEndpoint(listener.getsockname()[1], token)
            config = uvicorn.Config(app, log_level="error", ws="none")
        else:
            config = uvicorn.Config(
                app, uds=str(endpoint), log_level="error", ws="none"
            )
        server = FixtureServer(config)
        worker = Thread(
            target=server.run,
            kwargs={"sockets": [listener] if listener else None},
            daemon=True,
        )
        worker.start()
        try:
            if not server.ready.wait(5) or not worker.is_alive():
                raise AssertionError("In-test daemon did not start")
            if server.error is not None:
                raise server.error
            yield DaemonHandle(endpoint, 0, "fixture", "1")
        finally:
            app.state.event_broker.close()
            server.stop()
            worker.join(timeout=10)
            if listener is not None:
                listener.close()
            assert not worker.is_alive(), "In-test daemon survived shutdown"
            if server.error is not None:
                raise server.error
