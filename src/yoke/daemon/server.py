"""``yoke serve --daemon``: the per-user daemon process.

Start-up order matters for races: take the instance lock, import the runtime,
bind both listeners, publish ``daemon.json``, serve. Shutdown reverses it, and
removes the socket and record before the lock is released, so a client that
sees the lock free never finds a socket that belongs to a live daemon.
"""

from __future__ import annotations

import errno
import importlib
import os
import pkgutil
import secrets
import sqlite3
import socket
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import FrameType

import uvicorn

from yoke.daemon.build import build_id, protocol_version
from yoke.daemon.instance import DaemonInfo, InstanceLock, remove_info, write_info
from yoke.daemon.lifecycle import DEFAULT_GRACE_SECONDS, DaemonLifecycle
from yoke.daemon.log import (
    DETACHED_ENV,
    daemon_logging_config,
    detached_stdio,
    sanitized_stdio,
)
from yoke.daemon.paths import DEFAULT_PORT, DaemonPaths
from yoke.daemon.peer import peer_checked
from yoke.storage import DatabaseError

# Spawned daemons take no arguments, so these are read from the environment.
PORT_ENV = "YOKE_DAEMON_PORT"
GRACE_ENV = "YOKE_DAEMON_IDLE_GRACE"

# Everything a turn can reach. Imported before serving, so a checkout updated
# under a running daemon cannot mix new modules into the old process.
_RUNTIME_PACKAGES = (
    "yoke.agent",
    "yoke.agent_runs",
    "yoke.ai",
    "yoke.cli",
    "yoke.http",
    "yoke.mcp",
    "yoke.session",
    "yoke.storage",
)
# Terminal UI only; never imported by a daemon turn.
_SKIPPED_PACKAGES = ("yoke.cli.interactive", "yoke.cli.render")


def run_daemon_from_environment(
    *,
    port: int | None,
    idle_exit: bool,
    verbose: bool,
    session_directory: Path | None,
) -> int:
    """Run the daemon, taking an unset port and the grace period from the environment."""
    if port is None and (configured_port := os.environ.get(PORT_ENV)):
        port = int(configured_port)
    grace = float(os.environ.get(GRACE_ENV) or DEFAULT_GRACE_SECONDS)
    return run_daemon(
        port=port,
        idle_exit=idle_exit,
        grace_seconds=grace,
        verbose=verbose,
        session_directory=session_directory,
    )


def run_daemon(
    *,
    port: int | None = None,
    idle_exit: bool = True,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    verbose: bool = False,
    session_directory: Path | None = None,
    paths: DaemonPaths | None = None,
) -> int:
    """Run the daemon until it is idle, stopped or signalled. Losing the lock exits 0."""
    paths = paths or DaemonPaths.default()
    lock = InstanceLock.acquire(paths)
    if lock is None:
        print(f"Yoke daemon already running ({paths.lock})", file=sys.stderr)
        return 0
    unix_sock: socket.socket | None = None
    tcp_sock: socket.socket | None = None
    stdio = ExitStack()
    try:
        # Only the lifetime lock holder rotates; startup racers keep their
        # inherited stderr and cannot race the daemon's log renames.
        if os.environ.get(DETACHED_ENV) == "1":
            stdio.enter_context(detached_stdio(paths.log))
        preload_runtime()
        if sys.platform != "win32":
            unix_sock = _bind_unix(paths.socket)
        requested_port = DEFAULT_PORT if port is None else port
        tcp_sock = _bind_tcp(requested_port)
        if tcp_sock is None and port is None:
            print(
                f"WARNING: Yoke daemon default port {DEFAULT_PORT} is unavailable; "
                "falling back to an ephemeral loopback port. See yoke daemon status.",
                flush=True,
            )
            tcp_sock = _bind_tcp(0)
        if tcp_sock is None:
            raise OSError(f"Daemon loopback port {requested_port} is unavailable")
        selected_port = int(tcp_sock.getsockname()[1])
        token = os.environ.get("YOKE_HTTP_TOKEN") or secrets.token_urlsafe(32)
        lifecycle = DaemonLifecycle(idle_exit=idle_exit, grace_seconds=grace_seconds)
        try:
            server = _build_server(
                token=token,
                lifecycle=lifecycle,
                socket_path=paths.socket,
                port=selected_port,
                verbose=verbose,
                session_directory=session_directory,
            )
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            from yoke.session.repository.utils import database_path_for
            from yoke.storage.errors import database_open_error

            print(
                database_open_error(database_path_for(session_directory), exc),
                file=sys.stderr,
                flush=True,
            )
            return 1
        write_info(
            paths,
            DaemonInfo(
                pid=os.getpid(),
                build_id=build_id(),
                protocol_version=protocol_version(),
                socket=str(paths.socket) if unix_sock is not None else "",
                port=selected_port,
                token=token,
                idle_exit=idle_exit,
                started_at=time.time(),
            ),
        )
        print(f"Yoke daemon {build_id()} pid {os.getpid()}", flush=True)
        if unix_sock is not None:
            print(f"Yoke daemon socket {paths.socket}", flush=True)
        print(f"Yoke daemon listening on http://127.0.0.1:{selected_port}", flush=True)
        server.run(sockets=[s for s in (unix_sock, tcp_sock) if s is not None])
        print(f"Yoke daemon exiting: {lifecycle.exit_reason or 'signal'}", flush=True)
        return 0
    finally:
        for sock in (unix_sock, tcp_sock):
            if sock is not None:
                sock.close()
        if unix_sock is not None:
            paths.socket.unlink(missing_ok=True)
        remove_info(paths, pid=os.getpid())
        stdio.close()
        lock.release()


def preload_runtime() -> None:
    """Import every module a daemon turn can reach."""
    for package_name in _RUNTIME_PACKAGES:
        package = importlib.import_module(package_name)
        for module in pkgutil.walk_packages(package.__path__, f"{package_name}."):
            name = module.name
            if name.endswith(".__main__") or name.startswith(_SKIPPED_PACKAGES):
                continue
            # Fail startup rather than lazily importing changed code in a turn.
            importlib.import_module(name)


class _DaemonServer(uvicorn.Server):
    """Uvicorn server that ends SSE and lease streams before shutting down."""

    def __init__(
        self,
        config: uvicorn.Config,
        close_streams: Callable[[], None],
        redact: Callable[[str], str],
    ) -> None:
        super().__init__(config)
        self._close_streams = close_streams
        self._redact = redact

    def run(self, sockets: list[socket.socket] | None = None) -> None:
        with sanitized_stdio(self._redact):
            super().run(sockets=sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._close_streams()
        super().handle_exit(sig, frame)


def _build_server(
    *,
    token: str,
    lifecycle: DaemonLifecycle,
    socket_path: Path,
    port: int | None,
    verbose: bool,
    session_directory: Path | None,
) -> _DaemonServer:
    from yoke.http.app import HttpAppSettings, create_app
    from yoke.http.server import KEEPALIVE_TIMEOUT_SECONDS

    app = create_app(
        HttpAppSettings(
            auth_token=token,
            session_directory=session_directory,
            daemon=lifecycle,
            daemon_socket=str(socket_path) if sys.platform != "win32" else None,
        )
    )
    app.state.pending_input_service.hold_stranded()
    app.state.daemon_port = port
    config = uvicorn.Config(
        app,
        log_level="info" if verbose else "warning",
        log_config=daemon_logging_config(app.state.event_service.privacy.text),
        access_log=verbose,
        timeout_keep_alive=KEEPALIVE_TIMEOUT_SECONDS,
        timeout_graceful_shutdown=3,
    )
    config.load()
    config.http_protocol_class = peer_checked(config.http_protocol_class)
    broker = app.state.event_broker

    def close_streams() -> None:
        broker.close()
        lifecycle.request_exit("signal")

    server = _DaemonServer(config, close_streams, app.state.event_service.privacy.text)

    def on_exit(_reason: str) -> None:
        broker.close()
        server.should_exit = True

    lifecycle.on_exit(on_exit)
    return server


def _bind_unix(path: Path) -> socket.socket:
    """Bind the socket; any file at *path* is stale because we hold the lock."""
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        previous = os.umask(0o177)
        try:
            sock.bind(str(path))
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock


def _bind_tcp(port: int) -> socket.socket | None:
    """Bind loopback TCP for the browser; ``None`` when the port is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            return None
        raise
    return sock
