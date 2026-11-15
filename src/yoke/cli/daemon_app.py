"""`yoke daemon`: show, restart or stop the per-user daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

daemon_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Show, restart or stop the per-user Yoke daemon.",
)

_EXIT_TIMEOUT_SECONDS = 15.0


@daemon_app.command("status")
def status() -> None:
    """Print the running daemon's pid, build, listeners and leases (exit 1 if none)."""
    import httpx

    from yoke.daemon.build import build_id
    from yoke.daemon.client import find_daemon, unix_client
    from yoke.daemon.instance import daemon_alive, read_info
    from yoke.daemon.paths import DaemonPaths

    paths = DaemonPaths.default()
    socket_path = find_daemon(paths)
    if socket_path is None:
        typer.echo("Yoke daemon: not running")
        if daemon_alive(paths):
            typer.echo(f"  lock {paths.lock} is held but the socket does not answer")
        elif paths.info.exists() or paths.socket.exists():
            typer.echo("  stale daemon.json or socket; the next client replaces them")
        raise typer.Exit(1)
    try:
        with unix_client(socket_path) as client:
            data = client.get("daemon").raise_for_status().json()["data"]
    except httpx.HTTPError as exc:
        typer.echo(f"Yoke daemon: unreachable ({exc})", err=True)
        raise typer.Exit(1) from exc
    info = read_info(paths)
    client_build = build_id()
    port = data["port"]
    typer.echo(f"Yoke daemon: running, pid {data['pid']}")
    typer.echo(f"  build     {data['buildID']} (protocol {data['protocolVersion']})")
    if data["buildID"] != client_build:
        typer.echo(f"  client    {client_build} (differs)")
    typer.echo(f"  socket    {data['socket']}")
    typer.echo(
        f"  http      http://127.0.0.1:{port}"
        if port
        else "  http      none (socket-only)"
    )
    if info is not None:
        typer.echo(f"  token     in {paths.info}")
    typer.echo(f"  idle exit {'on' if data['idleExit'] else 'off'}")
    typer.echo(f"  busy      {'yes' if data['busy'] else 'no'}")
    if data["restartPending"]:
        typer.echo("  restart   pending, once turns finish")
    leases = data["leases"]
    typer.echo(f"  leases    {len(leases)}")
    for lease in leases:
        pid = f" pid {lease['pid']}" if lease.get("pid") else ""
        typer.echo(f"    {lease['client']}{pid}")


@daemon_app.command("restart")
def restart(
    when_idle: Annotated[
        bool,
        typer.Option("--when-idle", help="If busy, restart once running turns finish."),
    ] = False,
) -> None:
    """Restart the daemon on this checkout's code (starts one if none runs)."""
    from yoke.daemon.client import connect_or_spawn, find_daemon, unix_client
    from yoke.daemon.paths import DaemonPaths

    paths = DaemonPaths.default()
    socket_path = find_daemon(paths)
    if socket_path is not None:
        with unix_client(socket_path) as client:
            old_pid = client.get("daemon").raise_for_status().json()["data"]["pid"]
            response = client.post("daemon/restart", json={"whenIdle": when_idle})
        if response.status_code == 409:
            typer.echo("Yoke daemon is busy; retry with --when-idle.", err=True)
            raise typer.Exit(1)
        response.raise_for_status()
        if response.json()["data"]["scheduled"] == "when_idle":
            typer.echo(f"Yoke daemon {old_pid} restarts once its turns finish.")
            return
        _wait_until_gone(socket_path, old_pid)
    handle = connect_or_spawn(paths)
    typer.echo(f"Yoke daemon: running, pid {handle.pid}, build {handle.build_id}")


@daemon_app.command("stop")
def stop(
    force: Annotated[
        bool,
        typer.Option("--force", help="Stop even while turns are running."),
    ] = False,
) -> None:
    """Stop the daemon. Refuses while a turn runs unless --force."""
    from yoke.daemon.client import find_daemon, unix_client
    from yoke.daemon.paths import DaemonPaths

    socket_path = find_daemon(DaemonPaths.default())
    if socket_path is None:
        typer.echo("Yoke daemon: not running")
        return
    with unix_client(socket_path) as client:
        pid = client.get("daemon").raise_for_status().json()["data"]["pid"]
        response = client.post("daemon/stop", json={"force": force})
    if response.status_code == 409:
        typer.echo("Yoke daemon is busy; pass --force to stop it anyway.", err=True)
        raise typer.Exit(1)
    response.raise_for_status()
    _wait_until_gone(socket_path, pid)
    typer.echo(f"Yoke daemon {pid} stopped.")


def _wait_until_gone(socket_path: Path, pid: int) -> None:
    from yoke.daemon.client import DaemonUnavailable, wait_for_exit

    try:
        wait_for_exit(socket_path, pid, timeout=_EXIT_TIMEOUT_SECONDS)
    except DaemonUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
