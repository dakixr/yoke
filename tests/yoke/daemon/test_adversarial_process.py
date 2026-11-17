"""Adversarial process boundaries, isolated by the daemon sandbox fixture."""

from __future__ import annotations

import json
import subprocess
import sys


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
