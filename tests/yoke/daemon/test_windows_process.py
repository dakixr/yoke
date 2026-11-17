"""Real process checks shared by Windows CI and the Linux regression suite."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import httpx

from yoke.cli.backend.connection import SessionConnection

from tests.yoke.daemon.support import eventually
from yoke.acp.native import NativeClient
from yoke.daemon.client import find_daemon, DaemonHandle
from yoke.daemon.instance import InstanceLock, daemon_alive, read_info
from yoke.daemon.transport import TcpEndpoint
from yoke.daemon.lease import hold_lease


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "yoke", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_spawn_attach_second_client_auth_acp_and_idle_exit(sandbox, monkeypatch):
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "3")
    handle = sandbox.connect()
    info = read_info(sandbox.paths)
    assert info is not None and info.pid == handle.pid
    if sys.platform == "win32":
        assert handle.socket == TcpEndpoint(info.port, info.token)
        assert info.socket == ""
        assert not sandbox.paths.socket.exists()
    else:
        assert handle.socket == sandbox.paths.socket
        assert sandbox.paths.socket.stat().st_mode & 0o777 == 0o600
    connection = SessionConnection(
        "lease-proof", discover=lambda: handle, cwd=sandbox.root
    )
    with connection.startup_lease():
        assert any(
            lease["client"] == connection.owner for lease in sandbox.status()["leases"]
        )
        second = invoke("daemon", "status")
        assert second.returncode == 0, second.stderr
        assert f"pid {handle.pid}" in second.stdout
        loser = invoke("serve", "--daemon")
        assert loser.returncode == 0, loser.stderr
        assert "already running" in loser.stderr
        assert sandbox.status()["pid"] == handle.pid
        with httpx.Client(
            base_url=f"http://127.0.0.1:{info.port}/api/v1/", trust_env=False
        ) as http:
            for route in ("capabilities", "daemon", "session"):
                assert http.get(route).status_code == 401
                assert (
                    http.get(
                        route, headers={"Authorization": "Bearer wrong"}
                    ).status_code
                    == 401
                )
            assert http.post("daemon/stop", json={}).status_code == 401
            assert http.post("daemon/restart", json={}).status_code == 401

        async def acp() -> None:
            native = NativeClient(f"http://127.0.0.1:{info.port}", info.token)
            try:
                assert (await native.request("GET", "capabilities"))["data"]
            finally:
                await native.close()

        asyncio.run(acp())
    sandbox.wait_stopped(timeout=15)
    assert read_info(sandbox.paths) is None


def test_cli_restart_and_stop_refresh_acp_transport_and_release_lock(
    sandbox, monkeypatch
):
    from typer.testing import CliRunner
    from yoke.cli.main import app

    def control(*arguments: str) -> subprocess.CompletedProcess[str]:
        # Exercise the real parser and commands. Both daemons still run in fresh
        # processes; three extra CLI wrapper interpreters only add import load.
        result = CliRunner().invoke(app, list(arguments))
        return subprocess.CompletedProcess(
            arguments, result.exit_code, result.stdout, result.stderr
        )

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    original = sandbox.start("--no-idle-exit")
    endpoint = sandbox.connect().socket

    async def scenario():
        if isinstance(endpoint, TcpEndpoint):
            native = NativeClient(f"http://127.0.0.1:{endpoint.port}", endpoint.token)
        else:
            native = NativeClient("http://yoke", None, uds=str(endpoint))
        connected = asyncio.Event()

        def attach(handle: DaemonHandle):
            native.use_daemon_endpoint(handle.socket)
            connected.set()

        lease = asyncio.create_task(hold_lease("acp", sandbox.paths, on_connect=attach))
        try:
            await asyncio.wait_for(connected.wait(), 20)
            connected.clear()
            restarted = await asyncio.to_thread(control, "daemon", "restart")
            assert restarted.returncode == 0, restarted.stderr
            await asyncio.wait_for(connected.wait(), 20)
            assert (await native.data("GET", "daemon"))["pid"] != original
            # Observe the replacement so teardown waits on its process handle.
            assert sandbox.status()["pid"] != original
            stopped = await asyncio.to_thread(control, "daemon", "stop")
            assert stopped.returncode == 0, stopped.stderr
            await asyncio.wait_for(lease, 20)
        finally:
            lease.cancel()
            await asyncio.gather(lease, return_exceptions=True)
            await native.close()

    asyncio.run(scenario())
    sandbox.wait_stopped()
    assert find_daemon(sandbox.paths) is None
    assert control("daemon", "status").returncode == 1


def test_crashed_lock_holder_releases_os_lock(sandbox):
    # This child owns only the sandbox lock. Killing it also proves liveness
    # does not rely on stale PIDs in daemon.json or signal-zero on Windows.
    code = """
import time
from yoke.daemon.instance import InstanceLock
from yoke.daemon.paths import DaemonPaths
lock = InstanceLock.acquire(DaemonPaths.default())
assert lock is not None
print('locked', flush=True)
time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == b"locked"
        assert daemon_alive(sandbox.paths)
        assert InstanceLock.acquire(sandbox.paths, wait_seconds=0) is None
        process.kill()
        process.wait(timeout=10)
        eventually(lambda: not daemon_alive(sandbox.paths))
        lock = InstanceLock.acquire(sandbox.paths, wait_seconds=0)
        assert lock is not None
        lock.release()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def test_token_record_and_directory_are_private(sandbox):
    sandbox.paths.ensure()
    from yoke.daemon.instance import DaemonInfo, write_info

    write_info(
        sandbox.paths, DaemonInfo(os.getpid(), "test", "1", "", 1, "secret", True, 0)
    )
    if sys.platform != "win32":
        assert sandbox.paths.state_dir.stat().st_mode & 0o777 == 0o700
        assert sandbox.paths.info.stat().st_mode & 0o777 == 0o600
        return
    import csv

    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    sid = next(csv.reader(identity.stdout.splitlines()))[1]
    # Windows serializes the current local Administrator SID with its LA alias.
    canonical_sid = "LA" if sid.endswith("-500") else sid
    assert _windows_dacl(sandbox.paths.state_dir) == f"D:P(A;OICI;FA;;;{canonical_sid})"
    assert _windows_dacl(sandbox.paths.info) == f"D:P(A;;FA;;;{canonical_sid})"


def _windows_dacl(path):
    """Inspect the stored DACL through the native read API, independently of writes."""
    import ctypes
    from ctypes import wintypes

    advapi = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    advapi.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetFileSecurityW.restype = wintypes.BOOL
    size = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    assert advapi.GetFileSecurityW(str(path), 4, buffer, size, ctypes.byref(size))
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    output = wintypes.LPWSTR()
    assert convert(buffer, 1, 4, ctypes.byref(output), None)
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    try:
        return output.value
    finally:
        kernel.LocalFree(ctypes.cast(output, wintypes.LPVOID))


def test_detached_log_rotates_with_inherited_stdio(sandbox):
    sandbox.paths.ensure()
    code = """
import sys
from pathlib import Path
from yoke.daemon import log
log.MAX_LOG_BYTES = 1024
with log.detached_stdio(Path(sys.argv[1])):
    for _ in range(8):
        print("x" * 900)
"""
    from yoke.daemon.log import open_spawn_log

    with open_spawn_log(sandbox.paths.log) as output:
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(sandbox.paths.log)],
            stdout=output,
            stderr=output,
            env={**os.environ, "YOKE_DAEMON_DETACHED": "1"},
        )
    assert process.wait(timeout=15) == 0, sandbox.paths.log.read_text()
    files = list(sandbox.paths.log.parent.glob("daemon.log*"))
    assert len(files) == 3
    assert all(path.stat().st_size <= 1024 for path in files)
