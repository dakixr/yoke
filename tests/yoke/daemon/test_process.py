"""Proof of races, transport authorization, lifecycle and build skew."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from tests.yoke.daemon.support import DaemonSandbox, eventually
from yoke.daemon.client import DaemonIncompatible, connect_or_spawn, unix_client
from yoke.daemon.instance import read_info


def create_session(sandbox: DaemonSandbox) -> None:
    with unix_client(sandbox.paths.socket) as http:
        response = http.post(
            "session",
            json={
                "id": "proof",
                "title": "Proof",
                "location": {"directory": str(sandbox.root)},
                "selection": {"provider": "fixture", "model": "echo"},
            },
        )
        assert response.status_code == 200, response.text


def slow_turn(sandbox: DaemonSandbox) -> None:
    create_session(sandbox)
    with unix_client(sandbox.paths.socket) as http:
        response = http.post("session/proof/prompt", json={"prompt": {"text": "slow"}})
        assert response.status_code == 200, response.text
    try:
        eventually(lambda: (sandbox.root / "started").exists())
    except AssertionError:
        raise AssertionError(sandbox.paths.log.read_text()) from None


def test_twenty_discovery_spawns_choose_exactly_one_daemon(sandbox, monkeypatch):
    import yoke.daemon.client as discovery

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    spawned = []
    original = discovery.spawn_daemon

    def spawn(paths):
        process = original(paths)
        spawned.append(process)
        return process

    monkeypatch.setattr(discovery, "spawn_daemon", spawn)
    barrier = threading.Barrier(20)

    def attach(_index):
        barrier.wait()
        return connect_or_spawn(sandbox.paths)

    with ThreadPoolExecutor(max_workers=20) as pool:
        handles = list(pool.map(attach, range(20)))
    assert len({handle.pid for handle in handles}) == 1
    pid = handles[0].pid
    eventually(lambda: all(p.pid == pid or p.poll() == 0 for p in spawned))
    assert [p.pid for p in spawned if p.poll() is None] == [pid]
    assert sandbox.status()["pid"] == pid


@pytest.mark.parametrize("stale", ["socket", "record", "killed"])
def test_stale_state_recovers_under_lifetime_lock(sandbox, monkeypatch, stale):
    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "5")
    sandbox.paths.ensure()
    previous = None
    if stale == "socket":
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(sandbox.paths.socket))
    elif stale == "record":
        sandbox.paths.info.write_text('{"pid": 999999, "garbage": true}')
    else:
        previous = sandbox.connect().pid
        os.kill(previous, signal.SIGKILL)
        sandbox.wait_stopped()
        assert sandbox.paths.socket.exists()
        assert sandbox.paths.info.exists()
    # A lock file on disk is harmless; only a held flock excludes another daemon.
    sandbox.paths.lock.touch()
    handle = sandbox.connect()
    assert handle.pid != previous
    info = read_info(sandbox.paths)
    assert info and info.pid == handle.pid
    assert sandbox.paths.info.stat().st_mode & 0o777 == 0o600


def test_occupied_tcp_port_keeps_authorized_socket(sandbox, monkeypatch):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setenv("YOKE_DAEMON_PORT", str(occupied.getsockname()[1]))
        sandbox.start("--no-idle-exit")
        status = sandbox.status()
        assert status["port"] is None
        with unix_client(sandbox.paths.socket) as http:
            assert http.get("capabilities").status_code == 200
        assert "socket-only" in sandbox.paths.log.read_text()


def test_tcp_requires_token_while_socket_uses_peer_credentials(sandbox):
    sandbox.start("--no-idle-exit")
    status = sandbox.status()
    info = read_info(sandbox.paths)
    assert info
    with httpx.Client(base_url=f"http://127.0.0.1:{status['port']}/api/v1/") as http:
        assert http.get("capabilities").status_code == 401
        assert http.post("daemon/restart", json={}).status_code == 401
        assert http.post("daemon/stop", json={}).status_code == 401
        assert (
            http.get(
                "capabilities", headers={"Authorization": f"Bearer {info.token}"}
            ).status_code
            == 200
        )
    with unix_client(sandbox.paths.socket) as http:
        assert http.get("health").json()["data"]["buildID"] == "test-a"
        assert http.get("capabilities").status_code == 200


@pytest.mark.parametrize("route", ["daemon/lease?client=proof", "event?client=web"])
def test_last_lease_disconnect_starts_idle_grace(sandbox, route):
    sandbox.connect()
    with unix_client(sandbox.paths.socket) as http:
        with http.stream("GET", route) as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            next(lines)
            time.sleep(0.8)
            assert len(sandbox.status()["leases"]) == 1
        closed = time.monotonic()
        sandbox.wait_stopped()
        elapsed = time.monotonic() - closed
        assert 0.35 <= elapsed < 3


def test_daemon_survives_running_turn_without_clients(sandbox):
    sandbox.connect()
    slow_turn(sandbox)
    time.sleep(0.65)
    assert sandbox.status()["busy"]
    sandbox.wait_stopped()
    assert "exiting: idle" in sandbox.paths.log.read_text()


def test_retired_worker_keeps_daemon_alive_until_physical_drain(sandbox):
    sandbox.connect()
    slow_turn(sandbox)
    with unix_client(sandbox.paths.socket) as http:
        assert http.post("session/proof/interrupt").status_code == 200
    time.sleep(0.65)
    assert sandbox.status()["busy"]
    sandbox.wait_stopped()


def test_background_tool_process_stops_with_daemon(sandbox):
    sandbox.start("--no-idle-exit")
    create_session(sandbox)
    with unix_client(sandbox.paths.socket) as http:
        response = http.post(
            "session/proof/prompt", json={"prompt": {"text": "background"}}
        )
        assert response.status_code == 200, response.text
        assert (
            http.post("session/proof/wait", params={"timeoutMs": 5000}).json()["data"][
                "state"
            ]
            == "idle"
        )
    child = sandbox.root / "child.pid"
    eventually(child.exists)
    pid = int(child.read_text())
    assert Path(f"/proc/{pid}").exists()
    sandbox.stop()
    eventually(lambda: not Path(f"/proc/{pid}").exists())


def test_queued_prompt_without_runtime_prevents_idle_exit(sandbox):
    sandbox.connect()
    create_session(sandbox)
    with unix_client(sandbox.paths.socket) as http:
        admitted = http.post(
            "session/proof/prompt", json={"prompt": {"text": "queued"}, "resume": False}
        )
        assert admitted.status_code == 200
        time.sleep(0.8)
        assert sandbox.status()["busy"]
        assert http.post("daemon/restart", json={}).status_code == 409
        queue = http.get("session/proof/queue").json()["data"]
        removed = http.patch(
            "session/proof/queue",
            json={
                "expectedRevision": queue["revision"],
                "operations": [{"op": "remove", "id": admitted.json()["data"]["id"]}],
            },
        )
        assert removed.status_code == 200, removed.text
    sandbox.wait_stopped()


def test_no_idle_exit_stays_until_explicit_stop(sandbox):
    sandbox.start("--no-idle-exit")
    time.sleep(0.9)
    assert sandbox.status()["idleExit"] is False


def test_idle_build_skew_restarts_and_reattaches(sandbox, monkeypatch):
    sandbox.start("--no-idle-exit")
    before = sandbox.status()["pid"]
    monkeypatch.setenv("YOKE_DAEMON_BUILD_ID", "test-b")
    after = sandbox.connect()
    assert after.pid != before
    assert after.build_id == "test-b"
    assert after.warning is None


def test_busy_compatible_skew_warns_then_restarts_without_interrupt(
    sandbox, monkeypatch
):
    sandbox.start("--no-idle-exit")
    before = sandbox.status()["pid"]
    slow_turn(sandbox)
    monkeypatch.setenv("YOKE_DAEMON_BUILD_ID", "test-b")
    handle = sandbox.connect()
    assert handle.pid == before
    assert handle.warning and "restarts" in handle.warning
    assert sandbox.status()["restartPending"]
    with unix_client(sandbox.paths.socket) as http:
        assert http.post("daemon/stop", json={}).status_code == 409
        assert (
            http.post("session/proof/wait", params={"timeoutMs": 5000}).json()["data"][
                "state"
            ]
            == "idle"
        )
        messages = http.get("session/proof/message", params={"order": "asc"}).json()[
            "data"
        ]
        assert messages[-1]["content"][0]["text"] == "echo:slow"
    sandbox.wait_stopped()
    assert sandbox.connect().build_id == "test-b"


def test_busy_incompatible_skew_refuses_without_interrupt(sandbox, monkeypatch):
    sandbox.start("--no-idle-exit")
    slow_turn(sandbox)
    monkeypatch.setenv("YOKE_DAEMON_BUILD_ID", "test-b")
    monkeypatch.setenv("YOKE_DAEMON_PROTOCOL", "2")
    with pytest.raises(DaemonIncompatible):
        connect_or_spawn(sandbox.paths)
    assert not sandbox.status()["restartPending"]
    with unix_client(sandbox.paths.socket) as http:
        assert (
            http.post("session/proof/wait", params={"timeoutMs": 5000}).json()["data"][
                "state"
            ]
            == "idle"
        )


def test_acp_without_url_runs_initialize_new_and_prompt(sandbox):
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yoke",
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin = process.stdin
        stdout = process.stdout
        stderr = process.stderr
        if stdin is None or stdout is None or stderr is None:
            raise AssertionError("ACP subprocess pipes were not created")
        counter = 0
        updates = []

        async def request(method, params):
            nonlocal counter
            counter += 1
            stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": counter,
                        "method": method,
                        "params": params,
                    }
                ).encode()
                + b"\n"
            )
            await stdin.drain()
            while True:
                raw = await asyncio.wait_for(stdout.readline(), 15)
                assert raw, (await stderr.read()).decode()
                value = json.loads(raw)
                if value.get("id") == counter:
                    assert "error" not in value, value
                    return value["result"]
                updates.append(value)

        try:
            assert (await request("initialize", {"protocolVersion": 1}))[
                "protocolVersion"
            ] == 1
            created = await request(
                "session/new", {"cwd": str(sandbox.root), "mcpServers": []}
            )
            result = await request(
                "session/prompt",
                {
                    "sessionId": created["sessionId"],
                    "prompt": [{"type": "text", "text": "hello daemon"}],
                },
            )
            assert result["stopReason"] == "end_turn"
            assert "echo:hello daemon" in json.dumps(updates)
            assert sandbox.status()["leases"][0]["client"] == "acp"
        finally:
            stdin.close()
            await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, (await stderr.read()).decode()

    asyncio.run(scenario())
    sandbox.wait_stopped()
