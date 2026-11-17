"""Discover and attach CLI and ACP clients to the shared per-user daemon.

Unix uses its peer-checked socket; Windows uses the recorded loopback port and
bearer token. Apply the same version-skew policy before returning either.
"""

from __future__ import annotations

import os
import logging
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from enum import StrEnum
from typing import Any, Literal, TypedDict

import httpx

from yoke.daemon.build import build_id, fresh_build_id, newer_release, protocol_version
from yoke.daemon.environment import spawn_environment
from yoke.daemon.identity import identity_proof
from yoke.daemon.instance import daemon_alive, read_info
from yoke.daemon.log import open_spawn_log, startup_error
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.transport import Endpoint, TcpEndpoint, client_options
from yoke.session.repository.utils import default_database_path

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

    Automatically move only toward a strictly newer on-disk release. Different
    trees at the same version warn, so reconnecting leases cannot ping-pong.
    Compatible skew warns; incompatible skew refuses without interrupting work.
    """
    if client_build == daemon_build and client_protocol == daemon_protocol:
        return "attach"
    if not busy and newer_release(disk_build or client_build, daemon_build):
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
        if (
            info is None
            or info.port is None
            or not info.token
            or not daemon_alive(paths)
        ):
            return None
        try:
            endpoint = TcpEndpoint(info.port, info.token)
        except ValueError:
            return None
        if not _verified_tcp(endpoint):
            if info.protocol_version != protocol_version():
                raise DaemonIncompatible(
                    "The running Yoke daemon cannot prove its TCP identity with "
                    f"protocol {info.protocol_version}. Upgrade and restart that "
                    "daemon explicitly before attaching this client."
                )
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


class DiscoveryStep(StrEnum):
    """Each discovery has one restart edge, consumed before it is followed."""

    PROBE = "probe"
    SPAWN = "spawn"
    STATUS = "status"
    DECIDE = "decide"
    RESTART_ONCE = "restart_once"
    ATTACH = "attach"
    REFUSE = "refuse"


def _discovery_step(step: DiscoveryStep) -> None:
    logging.getLogger(__name__).debug("Daemon discovery: step=%s", step)


def connect_or_spawn(
    paths: DaemonPaths | None = None,
    *,
    timeout: float = SPAWN_TIMEOUT_SECONDS,
    allow_restart: bool = True,
) -> DaemonHandle:
    """Discover through bounded steps, consuming at most one restart attempt."""
    paths = paths or DaemonPaths.default()
    step = DiscoveryStep.PROBE
    socket_path: Endpoint | None = None
    status: dict[str, Any] = {}
    action: SkewAction = "attach"
    restart_available = allow_restart
    # Longest path: probe/spawn/status/decide/restart, then probe/spawn/status/
    # decide/attach. A failed or raced restart consumes the same single edge.
    for _ in range(10):
        _discovery_step(step)
        match step:
            case DiscoveryStep.PROBE:
                socket_path = find_daemon(paths)
                step = (
                    DiscoveryStep.STATUS
                    if socket_path is not None
                    else DiscoveryStep.SPAWN
                )
            case DiscoveryStep.SPAWN:
                socket_path = _spawn_and_wait(paths, timeout)
                step = DiscoveryStep.STATUS
            case DiscoveryStep.STATUS:
                assert socket_path is not None
                with unix_client(socket_path) as client:
                    status = _data(client.get("daemon"))
                _check_database(status)
                step = DiscoveryStep.DECIDE
            case DiscoveryStep.DECIDE:
                action = skew_action(
                    client_build=build_id(),
                    disk_build=fresh_build_id(),
                    client_protocol=protocol_version(),
                    daemon_build=status["buildID"],
                    daemon_protocol=status["protocolVersion"],
                    busy=bool(status["busy"]),
                )
                if action == "restart" and not restart_available:
                    action = (
                        "warn"
                        if status["protocolVersion"] == protocol_version()
                        else "refuse"
                    )
                step = (
                    DiscoveryStep.RESTART_ONCE
                    if action == "restart"
                    else DiscoveryStep.REFUSE
                    if action == "refuse"
                    else DiscoveryStep.ATTACH
                )
            case DiscoveryStep.RESTART_ONCE:
                assert socket_path is not None
                restart_available = False
                restart_daemon(socket_path, status["pid"], timeout=timeout, paths=paths)
                step = DiscoveryStep.PROBE
            case DiscoveryStep.REFUSE:
                raise DaemonIncompatible(
                    f"The running Yoke daemon ({status['buildID']}, protocol "
                    f"{status['protocolVersion']}) is incompatible with this "
                    f"client ({build_id()}, protocol {protocol_version()}). "
                    "Upgrade/restart the older client or daemon explicitly, or retry once "
                    "turns finish if a newer on-disk release is available."
                )
            case DiscoveryStep.ATTACH:
                assert socket_path is not None
                if status.get("databasePath") is None:
                    raise DaemonIncompatible(
                        "The running Yoke daemon does not report its database path. "
                        "Upgrade and restart the daemon before attaching."
                    )
                warning = None
                if action == "warn":
                    warning = (
                        f"Yoke daemon runs {status['buildID']}, this client is {build_id()}; "
                        "attached with compatible protocol. Automatic restart requires a newer "
                        "release on disk; align client and daemon versions to remove this warning."
                    )
                return DaemonHandle(
                    socket=socket_path,
                    pid=int(status["pid"]),
                    build_id=status["buildID"],
                    protocol_version=status["protocolVersion"],
                    warning=warning,
                )
    raise DaemonUnavailable("Yoke daemon discovery exceeded its step limit")


def _check_database(status: dict[str, Any]) -> None:
    database_path = status.get("databasePath")
    if database_path is None:
        return
    requested = default_database_path().resolve()
    actual = Path(database_path).expanduser().resolve()
    if requested != actual:
        raise DaemonUnavailable(
            f"Yoke database mismatch: client requested {requested}, but the "
            f"running daemon uses {actual}. Use the daemon database path or "
            "stop it explicitly before starting with another YOKE_DB_PATH."
        )


def restart_daemon(
    socket_path: Endpoint, pid: int, *, timeout: float, paths: DaemonPaths | None = None
) -> bool:
    """Wait for a requested restart; return False if work delays shutdown."""
    with unix_client(socket_path) as client:
        response = client.post("daemon/restart", json={})
        if response.status_code == 409:
            return False
        _data(response)
    try:
        wait_for_exit(socket_path, pid, timeout=timeout, paths=paths)
    except DaemonUnavailable:
        # Pending drains and older daemons can race an admission. Rediscover
        # once with restart consumed, then use the compatible warn/refuse rule.
        return False
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


def _verified_tcp(endpoint: TcpEndpoint) -> bool:
    """Challenge the listener without disclosing our bearer capability first."""
    nonce = secrets.token_hex(32)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{endpoint.port}/api/v1/",
            timeout=2.0,
            trust_env=False,
        ) as client:
            response = client.get("daemon/identity", params={"nonce": nonce})
            proof = _data(response)["proof"]
        return isinstance(proof, str) and secrets.compare_digest(
            proof, identity_proof(endpoint.token, nonce, endpoint.port)
        )
    except (httpx.HTTPError, DaemonUnavailable, KeyError, ValueError, TypeError):
        return False


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
