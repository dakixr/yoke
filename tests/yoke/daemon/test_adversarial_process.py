"""Adversarial process boundaries, isolated by the daemon sandbox fixture."""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest

from tests.yoke.daemon.support import eventually
from yoke.daemon.client import unix_client
from yoke.daemon.instance import read_info


def test_real_client_processes_race_to_one_daemon(sandbox, monkeypatch):
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    barrier = sandbox.root / "go"
    code = """
import json, sys, time
from pathlib import Path
from yoke.daemon.client import connect_or_spawn
while not Path(sys.argv[1]).exists():
    time.sleep(0.01)
h = connect_or_spawn(timeout=15)
print(json.dumps({'pid': h.pid, 'socket': str(h.socket)}))
"""
    clients = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(barrier)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
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


def test_peer_check_rejects_every_unix_connection(sandbox):
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
        for route in ("health", "daemon/restart", "event?client=foreign"):
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


def test_daemon_cli_exit_codes_and_busy_controls(sandbox, monkeypatch):
    from tests.yoke.daemon.test_process import create_session

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    from yoke.daemon.client import connect_or_spawn

    monkeypatch.setattr(
        connect_or_spawn,
        "__kwdefaults__",
        {**(connect_or_spawn.__kwdefaults__ or {}), "timeout": 15},
    )

    def cli(*args):
        from typer.testing import CliRunner
        from yoke.cli.main import app

        result = CliRunner().invoke(app, ["daemon", *args])
        return subprocess.CompletedProcess(
            args, result.exit_code, result.stdout, result.stderr
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
