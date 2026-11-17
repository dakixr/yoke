"""Find, start and attach to the per-user daemon over its Unix socket.

``connect_or_spawn`` is what every client (ACP now, the CLI later) calls: it
connects to a running daemon or spawns a detached one, then applies the
version-skew policy before handing back the socket to talk to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from yoke.daemon.build import build_id, protocol_version
from yoke.daemon.instance import daemon_alive, read_info
from yoke.daemon.log import DETACHED_ENV
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.uds import unix_transport

SPAWN_TIMEOUT_SECONDS = 5.0
_BASE_URL = "http://yoke/api/v1/"

type SkewAction = Literal["attach", "restart", "warn", "refuse"]


class DaemonUnavailable(RuntimeError):
    """No daemon answered, and none could be started in time."""


class DaemonIncompatible(RuntimeError):
    """A busy daemon speaks a protocol this client cannot use."""


@dataclass(frozen=True, slots=True)
class DaemonHandle:
    """An attached daemon: where it listens and what the client was told."""

    socket: Path
    pid: int
    build_id: str
    protocol_version: str
    warning: str | None = None


def unix_client(socket_path: Path, *, timeout: float = 5.0) -> httpx.Client:
    """Return a synchronous API client bound to the daemon socket."""
    return httpx.Client(
        transport=unix_transport(socket_path),
        base_url=_BASE_URL,
        timeout=timeout,
        trust_env=False,
    )


def skew_action(
    *,
    client_build: str,
    client_protocol: str,
    daemon_build: str,
    daemon_protocol: str,
    busy: bool,
) -> SkewAction:
    """Decide how a client attaches to a daemon built from other code.

    Idle: restart it. Busy: attach with a warning when the protocol matches,
    otherwise refuse. Another client's turn is never interrupted.
    """
    if client_build == daemon_build and client_protocol == daemon_protocol:
        return "attach"
    if not busy:
        return "restart"
    return "warn" if client_protocol == daemon_protocol else "refuse"


def find_daemon(paths: DaemonPaths | None = None) -> Path | None:
    """Return the socket of a daemon that answers ``/health``, if any.

    The socket recorded in ``daemon.json`` is tried too, for a client whose
    ``XDG_RUNTIME_DIR`` differs from the daemon's.
    """
    paths = paths or DaemonPaths.default()
    candidates = [paths.socket]
    info = read_info(paths)
    if info is not None and Path(info.socket) != paths.socket:
        candidates.append(Path(info.socket))
    for candidate in candidates:
        if _healthy(candidate):
            return candidate
    return None


def spawn_daemon(paths: DaemonPaths | None = None) -> subprocess.Popen[bytes]:
    """Start a detached ``yoke serve --daemon`` logging to ``~/.yoke/run``."""
    paths = paths or DaemonPaths.default()
    paths.ensure()
    with open(paths.log, "ab") as log:
        process = subprocess.Popen(
            # -P: never import from the working directory.
            [sys.executable, "-P", "-m", "yoke", "serve", "--daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=paths.state_dir,
            env={**os.environ, DETACHED_ENV: "1"},
        )
    # Reap it if it exits while this client lives (a losing racer exits at once).
    threading.Thread(
        target=process.wait, name="yoke-daemon-reaper", daemon=True
    ).start()
    return process


def connect_or_spawn(
    paths: DaemonPaths | None = None,
    *,
    timeout: float = SPAWN_TIMEOUT_SECONDS,
    allow_restart: bool = True,
) -> DaemonHandle:
    """Attach to the daemon, starting it first when none answers."""
    paths = paths or DaemonPaths.default()
    socket_path = find_daemon(paths) or _spawn_and_wait(paths, timeout)
    with unix_client(socket_path) as client:
        status = _data(client.get("daemon"))
    action = skew_action(
        client_build=build_id(),
        client_protocol=protocol_version(),
        daemon_build=status["buildID"],
        daemon_protocol=status["protocolVersion"],
        busy=bool(status["busy"]),
    )
    if action == "restart" and not allow_restart:
        # A restart already happened and another build won the lock.
        action = "warn" if status["protocolVersion"] == protocol_version() else "refuse"
    if action == "restart":
        restarted = restart_daemon(
            socket_path, status["pid"], timeout=timeout, paths=paths
        )
        if not restarted:
            # A turn admitted after the status read gets the busy skew policy.
            return connect_or_spawn(paths, timeout=timeout, allow_restart=allow_restart)
        return connect_or_spawn(paths, timeout=timeout, allow_restart=False)
    if action == "refuse":
        raise DaemonIncompatible(
            f"The running Yoke daemon ({status['buildID']}, protocol "
            f"{status['protocolVersion']}) is busy and incompatible with this "
            f"client ({build_id()}, protocol {protocol_version()}). "
            "Retry once its turns finish."
        )
    warning = None
    if action == "warn":
        with unix_client(socket_path) as client:
            client.post("daemon/restart", json={"whenIdle": True})
        warning = (
            f"Yoke daemon runs {status['buildID']}, this client is {build_id()}; "
            "it restarts once its running turns finish."
        )
    return DaemonHandle(
        socket=socket_path,
        pid=int(status["pid"]),
        build_id=status["buildID"],
        protocol_version=status["protocolVersion"],
        warning=warning,
    )


def restart_daemon(
    socket_path: Path, pid: int, *, timeout: float, paths: DaemonPaths | None = None
) -> bool:
    """Restart an idle daemon; return False if an admission made it busy."""
    with unix_client(socket_path) as client:
        response = client.post("daemon/restart", json={})
        if response.status_code == 409:
            return False
        _data(response)
    wait_for_exit(socket_path, pid, timeout=timeout, paths=paths)
    return True


def wait_for_exit(
    socket_path: Path, pid: int, *, timeout: float, paths: DaemonPaths | None = None
) -> None:
    """Wait for the lifetime lock, not just an unavailable HTTP listener."""
    paths = paths or DaemonPaths.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_pid = _daemon_pid(socket_path)
        if not daemon_alive(paths) or (current_pid is not None and current_pid != pid):
            return
        time.sleep(0.05)
    raise DaemonUnavailable(f"Yoke daemon {pid} did not exit within {timeout:.0f}s")


def _spawn_and_wait(paths: DaemonPaths, timeout: float) -> Path:
    try:
        spawn_daemon(paths)
    except OSError as exc:
        raise DaemonUnavailable(f"Could not start Yoke daemon: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = find_daemon(paths)
        if found is not None:
            return found
        time.sleep(0.05)
    raise DaemonUnavailable(
        f"Yoke daemon did not start within {timeout:.0f}s; see {paths.log}"
    )


def _healthy(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    try:
        with unix_client(socket_path, timeout=2.0) as client:
            response = client.get("health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _daemon_pid(socket_path: Path) -> int | None:
    try:
        with unix_client(socket_path, timeout=1.0) as client:
            return int(_data(client.get("daemon"))["pid"])
    except (httpx.HTTPError, DaemonUnavailable, KeyError, ValueError):
        return None


def _data(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        raise DaemonUnavailable(
            f"Yoke daemon answered {response.status_code}: {response.text[:200]}"
        )
    return response.json()["data"]
