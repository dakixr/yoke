"""Adversarial process boundaries, isolated by the daemon sandbox fixture."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.yoke.daemon.support import eventually
from tests.yoke.daemon.test_process import slow_turn
from yoke.daemon.client import unix_client
from yoke.daemon.instance import daemon_alive, read_info


def test_eight_real_client_processes_race_to_one_daemon(sandbox, monkeypatch):
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    barrier = sandbox.root / "go"
    code = """
import json, sys, time
from pathlib import Path
from yoke.daemon.client import connect_or_spawn
while not Path(sys.argv[1]).exists():
    time.sleep(0.01)
h = connect_or_spawn()
print(json.dumps({'pid': h.pid, 'socket': str(h.socket)}))
"""
    clients = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(barrier)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(8)
    ]
    try:
        barrier.touch()
        results = []
        for client in clients:
            out, err = client.communicate(timeout=20)
            assert client.returncode == 0, err.decode()
            results.append(json.loads(out))
        assert {r["pid"] for r in results} == {sandbox.status()["pid"]}
        assert {r["socket"] for r in results} == {str(sandbox.paths.socket)}
    finally:
        for client in clients:
            if client.poll() is None:
                client.kill()
            client.wait()


def test_crash_after_lock_before_bind_recovers(sandbox):
    marker = sandbox.root / "locked"
    code = """
import sys, time
from pathlib import Path
import yoke.daemon.server as server
def pause():
    Path(sys.argv[1]).touch()
    time.sleep(60)
server.preload_runtime = pause
server.run_daemon(port=0)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(marker)])
    try:
        eventually(marker.exists)
        assert daemon_alive(sandbox.paths)
        process.kill()
        process.wait(timeout=5)
        assert not daemon_alive(sandbox.paths)
        assert sandbox.connect().pid != process.pid
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_zombie_releases_lifetime_lock(sandbox):
    code = """
from yoke.daemon.instance import InstanceLock
from yoke.daemon.paths import DaemonPaths
lock = InstanceLock.acquire(DaemonPaths.default())
assert lock
"""
    process = subprocess.Popen([sys.executable, "-c", code])
    try:
        stat = f"/proc/{process.pid}/stat"
        eventually(lambda: Path(stat).read_text().split()[2] == "Z")
        assert not daemon_alive(sandbox.paths)
        assert sandbox.connect().pid != process.pid
    finally:
        process.wait(timeout=5)


@pytest.mark.parametrize("route", ["health", "daemon/restart", "event?client=foreign"])
def test_peer_check_rejects_every_unix_connection(sandbox, route):
    # Substitute only the kernel credential result in a real server. This also
    # proves public health and streaming routes cannot bypass the protocol check.
    code = """
import os
import yoke.daemon.peer as peer
from yoke.daemon.server import run_daemon
peer.peer_uid = lambda sock: os.getuid() + 1
run_daemon(port=0, idle_exit=False)
"""
    sandbox.paths.ensure()
    with sandbox.paths.log.open("ab") as log:
        process = subprocess.Popen([sys.executable, "-c", code], stdout=log, stderr=log)
    try:
        info = eventually(lambda: read_info(sandbox.paths))
        with unix_client(sandbox.paths.socket) as http:
            with pytest.raises(httpx.HTTPError):
                if route == "daemon/restart":
                    http.post(route, json={})
                else:
                    http.get(route)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{info.port}/api/v1/", trust_env=False
        ) as http:
            assert http.get("health").status_code == 200
            assert http.post("daemon/restart", json={}).status_code == 401
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_interrupts_inflight_turn_and_preserves_session(sandbox, sig):
    sandbox.start("--no-idle-exit")
    before = sandbox.status()["pid"]
    slow_turn(sandbox)
    os.kill(before, sig)
    sandbox.wait_stopped()
    sandbox.start("--no-idle-exit")
    with unix_client(sandbox.paths.socket) as http:
        status = http.post("session/proof/wait", params={"timeoutMs": 1}).json()["data"]
        assert status["state"] == "idle"
        messages = http.get("session/proof/message").json()["data"]
        assert any(m["type"] == "user" for m in messages)
        assert "interrupted" in json.dumps(messages)
        assert not sandbox.status()["busy"]


def test_lease_from_killed_client_is_released(sandbox):
    handle = sandbox.connect()
    code = """
import sys
from pathlib import Path
from yoke.daemon.client import unix_client
with unix_client(Path(sys.argv[1]), timeout=30) as http:
    with http.stream('GET', 'daemon/lease?client=killed') as response:
        for line in response.iter_lines():
            if line:
                print('ready', flush=True)
"""
    client = subprocess.Popen(
        [sys.executable, "-c", code, str(handle.socket)], stdout=subprocess.PIPE
    )
    try:
        assert client.stdout is not None
        assert client.stdout.readline() == b"ready\n"
        assert len(sandbox.status()["leases"]) == 1
        client.kill()
        client.wait(timeout=5)
        eventually(lambda: not sandbox.status().get("leases"))
        sandbox.wait_stopped()
    finally:
        if client.poll() is None:
            client.kill()
        client.wait()


def test_daemon_cli_exit_codes_and_busy_controls(sandbox, monkeypatch):
    from tests.yoke.daemon.test_process import create_session

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "yoke", "daemon", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )

    absent = cli("status")
    assert absent.returncode == 1 and "not running" in absent.stdout
    assert cli("stop").returncode == 0
    assert cli("restart").returncode == 0
    running = cli("status")
    assert running.returncode == 0 and "pid " in running.stdout
    create_session(sandbox)
    with unix_client(sandbox.paths.socket) as http:
        assert (
            http.post(
                "session/proof/prompt",
                json={"prompt": {"text": "queued"}, "resume": False},
            ).status_code
            == 200
        )
    for command in ("stop", "restart"):
        refused = cli(command)
        assert refused.returncode == 1
        assert "busy" in refused.stderr
    assert cli("stop", "--force").returncode == 0
    assert cli("status").returncode == 1


def test_stale_record_with_reused_live_pid_does_not_signal_it(sandbox):
    from yoke.daemon.instance import DaemonInfo, write_info

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        sandbox.paths.ensure()
        write_info(
            sandbox.paths,
            DaemonInfo(
                pid=sleeper.pid,
                build_id="stale",
                protocol_version="1",
                socket=str(sandbox.paths.socket),
                port=None,
                token="stale-token",
                idle_exit=True,
                started_at=0,
            ),
        )
        assert sandbox.connect().pid != sleeper.pid
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)
