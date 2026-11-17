"""Discover and attach CLI and ACP clients to the shared per-user daemon.

Unix uses its peer-checked socket; Windows uses the recorded loopback port and
bearer token. Apply the same version-skew policy before returning either.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx

from yoke.daemon.build import build_id, fresh_build_id, protocol_version
from yoke.daemon.environment import spawn_environment
from yoke.daemon.instance import daemon_alive, read_info
from yoke.daemon.log import open_spawn_log, startup_error
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.transport import Endpoint, TcpEndpoint, client_options

SPAWN_TIMEOUT_SECONDS = 5.0

type SkewAction = Literal["attach", "restart", "warn", "refuse"]


class DaemonUnavailable(RuntimeError):
    """No daemon answered, and none could be started in time."""


class DaemonIncompatible(RuntimeError):
    """A busy daemon speaks a protocol this client cannot use."""


@dataclass(frozen=True, slots=True)
class DaemonHandle:
    """An attached daemon: where it listens and what the client was told."""

    socket: Endpoint
    pid: int
    build_id: str
    protocol_version: str
    warning: str | None = None


def unix_client(socket_path: Endpoint, *, timeout: float = 5.0) -> httpx.Client:
    """Return a local API client, preserving the historical Unix helper name."""
    return httpx.Client(
        **client_options(socket_path),
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
    disk_build: str | None = None,
) -> SkewAction:
    """Decide how a client attaches to a daemon built from other code.

    Restart only an idle daemon that differs from a fresh spawn. Compatible
    skew attaches with a warning; incompatible skew refuses.
    """
    if client_build == daemon_build and client_protocol == daemon_protocol:
        return "attach"
    if not busy and daemon_build != (disk_build or client_build):
        return "restart"
    return "warn" if client_protocol == daemon_protocol else "refuse"


def find_daemon(paths: DaemonPaths | None = None) -> Endpoint | None:
    """Return a healthy local endpoint, authenticating Windows TCP discovery.

    The socket recorded in ``daemon.json`` is tried too, for a client whose
    ``XDG_RUNTIME_DIR`` differs from the daemon's.
    """
    paths = paths or DaemonPaths.default()
    info = read_info(paths)
    if sys.platform == "win32":
        if info is None or info.port is None or not info.token:
            return None
        try:
            endpoint = TcpEndpoint(info.port, info.token)
        except ValueError:
            return None
        return endpoint if _healthy(endpoint) else None
    candidates = [paths.socket]
    if info is not None and Path(info.socket) != paths.socket:
        candidates.append(Path(info.socket))
    for candidate in candidates:
        if _healthy(candidate):
            return candidate
    return None


class SpawnOptions(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


def spawn_options() -> SpawnOptions:
    """Detach without a console on Windows, preserving POSIX session creation."""
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, also testable on Unix.
        return {"creationflags": 0x00000008 | 0x00000200}
    return {"start_new_session": True}


def spawn_daemon(paths: DaemonPaths | None = None) -> subprocess.Popen[bytes]:
    """Start a detached ``yoke serve --daemon`` logging to ``~/.yoke/run``."""
    paths = paths or DaemonPaths.default()
    paths.ensure()
    with open_spawn_log(paths.log) as log:
        process = subprocess.Popen(
            # -P: never import from the working directory.
            [sys.executable, "-P", "-m", "yoke", "serve", "--daemon"],
            text=False,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **spawn_options(),
            close_fds=True,
            cwd=paths.state_dir,
            env=spawn_environment(os.environ),
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
        disk_build=fresh_build_id(),
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
            return connect_or_spawn(paths, timeout=timeout, allow_restart=False)
        return connect_or_spawn(paths, timeout=timeout, allow_restart=False)
    if action == "refuse":
        raise DaemonIncompatible(
            f"The running Yoke daemon ({status['buildID']}, protocol "
            f"{status['protocolVersion']}) is incompatible with this "
            f"client ({build_id()}, protocol {protocol_version()}). "
            "Restart the client to load current code, or retry once turns finish."
        )
    warning = None
    if action == "warn":
        warning = (
            f"Yoke daemon runs {status['buildID']}, this client is {build_id()}; "
            "attached with compatible protocol. Restart the client to load current code."
        )
    return DaemonHandle(
        socket=socket_path,
        pid=int(status["pid"]),
        build_id=status["buildID"],
        protocol_version=status["protocolVersion"],
        warning=warning,
    )


def restart_daemon(
    socket_path: Endpoint, pid: int, *, timeout: float, paths: DaemonPaths | None = None
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
    socket_path: Endpoint, pid: int, *, timeout: float, paths: DaemonPaths | None = None
) -> None:
    """Wait for the lifetime lock, not just an unavailable HTTP listener."""
    paths = paths or DaemonPaths.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # TCP restarts can select a new port and always rotate the token. A
        # lease holder may already have spawned the replacement and taken its
        # lock before this waiter observes the old lock being released.
        endpoint = (
            (find_daemon(paths) or socket_path)
            if isinstance(socket_path, TcpEndpoint)
            else socket_path
        )
        current_pid = _daemon_pid(endpoint)
        if not daemon_alive(paths) or (current_pid is not None and current_pid != pid):
            return
        time.sleep(0.05)
    raise DaemonUnavailable(f"Yoke daemon {pid} did not exit within {timeout:.0f}s")


def _spawn_and_wait(paths: DaemonPaths, timeout: float) -> Endpoint:
    started_ns = time.time_ns()
    try:
        process = spawn_daemon(paths)
    except OSError as exc:
        raise DaemonUnavailable(f"Could not start Yoke daemon: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = find_daemon(paths)
        if found is not None:
            return found
        if process.poll() is not None:
            error = startup_error(paths.log, since_ns=started_ns)
            if error is not None:
                raise DaemonUnavailable(error)
        time.sleep(0.05)
    error = startup_error(paths.log, since_ns=started_ns)
    if error is not None:
        raise DaemonUnavailable(error)
    raise DaemonUnavailable(
        f"Yoke daemon did not start within {timeout:.0f}s; see {paths.log}"
    )


def _healthy(socket_path: Endpoint) -> bool:
    if isinstance(socket_path, Path) and not socket_path.exists():
        return False
    try:
        with unix_client(socket_path, timeout=2.0) as client:
            response = client.get(
                "capabilities" if isinstance(socket_path, TcpEndpoint) else "health"
            )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _daemon_pid(socket_path: Endpoint) -> int | None:
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
