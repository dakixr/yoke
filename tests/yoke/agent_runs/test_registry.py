"""Run state, heartbeat revisions, capability ownership and retention."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import threading

import pytest

from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.context import Binding
from yoke.agent_runs.protocol import ENVIRONMENT_KEY, RegistrationError, request
from yoke.agent_runs.records import Owner


def registration(run: str = "run", agent: str = "agent") -> dict[str, object]:
    return {
        "op": "register",
        "runId": run,
        "agentId": agent,
        "name": "reviewer",
        "provider": "fixture",
        "model": "test",
    }


@pytest.fixture
def registry():
    value = AgentRunRegistry()
    try:
        yield value
    finally:
        value.close()


def test_registry_resources_are_lazy_and_close_revokes_authority(registry):
    assert registry.snapshots() == []
    unsubscribe = registry.subscribe(lambda: None)
    assert registry._server is None and registry._monitor is None
    token = registry.capability(Owner("owner", 123))
    assert registry._server is None and registry._monitor is None
    registry.close()
    unsubscribe()
    with pytest.raises(RuntimeError):
        registry.dispatch({**registration(), "token": token})


def test_fake_clock_heartbeats_reconnect_and_meaningful_revisions():
    now = [0.0]
    registry = AgentRunRegistry(clock=lambda: now[0], stale_after=5, lost_after=10)
    notifications = []
    condition = threading.Condition()

    def callback():
        with condition:
            notifications.append(registry.snapshots())
            condition.notify_all()

    def wait_for_notifications(count):
        with condition:
            assert condition.wait_for(lambda: len(notifications) == count, timeout=2)

    registry.subscribe(callback)
    root = registry.capability(Owner("owner", 42))
    try:
        token = registry.dispatch({**registration(), "token": root})["token"]
        first = registry.snapshots()[0]
        wait_for_notifications(1)
        registry.dispatch({"op": "report", "token": token, "sequence": 1})
        assert registry.snapshots()[0]["version"] == first["version"]
        assert len(notifications) == 1
        now[0] = 6
        registry.reconcile()
        assert registry.snapshots()[0]["observation"] == "stale"
        wait_for_notifications(2)
        now[0] = 11
        registry.reconcile()
        lost = registry.snapshots()[0]
        assert lost["observation"] == "lost"
        assert lost["status"] == "running" and lost["finishedAt"] is None
        wait_for_notifications(3)
        registry.dispatch({"op": "report", "token": token, "sequence": 2})
        assert registry.snapshots()[0]["observation"] == "live"
        wait_for_notifications(4)
        assert len(notifications) == 4
        registry.dispatch(
            {"op": "report", "token": token, "sequence": 1, "status": "failed"}
        )
        assert len(notifications) == 4
    finally:
        registry.close()


def test_duplicates_terminals_privacy_and_host_authority(registry):
    root = registry.capability(Owner("real-owner", 321, "parent-run", "parent-agent"))
    frame = {
        **registration(),
        "token": root,
        "sessionID": "forged-owner",
        "runtimeSessionID": 999,
        "prompt": "SECRET_PROMPT",
        "output": "SECRET_OUTPUT",
        "credentials": "SECRET_CREDENTIAL",
    }
    ack = registry.dispatch(frame)
    assert registry.dispatch(frame) == ack
    row = registry.snapshots()[0]
    assert row["sessionID"] == "real-owner" and row["runtimeSessionID"] == 321
    assert row["parentRunId"] == "parent-run" and row["parentAgentId"] == "parent-agent"
    registry.dispatch(
        {
            "op": "report",
            "token": ack["token"],
            "sequence": 2,
            "status": "failed",
            "errorType": "ValueError",
            "error": "SECRET_ERROR",
        }
    )
    terminal = registry.snapshots()
    registry.dispatch(
        {"op": "report", "token": ack["token"], "sequence": 3, "status": "running"}
    )
    assert registry.snapshots() == terminal
    assert "SECRET" not in json.dumps(terminal)
    with pytest.raises(ValueError):
        registry.dispatch({**frame, "agentId": "different"})
    with pytest.raises(ValueError):
        registry.dispatch({**frame, "token": "not-the-capability"})


def test_callbacks_are_outside_lock_and_errors_do_not_break_registration(registry):
    observed = threading.Event()
    completed = threading.Event()

    def inspect_from_another_thread():
        registry.snapshots()
        completed.set()

    def callback():
        thread = threading.Thread(target=inspect_from_another_thread)
        thread.start()
        assert completed.wait(2)
        thread.join()
        observed.set()
        raise RuntimeError("observer failed")

    unsubscribe = registry.subscribe(callback)
    root = registry.capability(Owner())
    registry.dispatch({**registration(), "token": root})
    assert observed.wait(2)
    unsubscribe()


def test_slow_subscriber_does_not_hold_registration_ack(registry):
    entered = threading.Event()
    release = threading.Event()

    def callback():
        entered.set()
        assert release.wait(3)

    unsubscribe = registry.subscribe(callback)
    root = registry.capability(Owner("owner"))
    host = Binding(token=root, registry=registry)
    try:
        host.call(registration("first"))
        assert entered.wait(2)
        host.call(registration("second"))
        assert len(registry.snapshots()) == 2
    finally:
        unsubscribe()
        release.set()


def test_retention_never_evicts_running_work():
    registry = AgentRunRegistry(history_limit=2)
    root = registry.capability(Owner())
    try:
        registry.dispatch({**registration("live"), "token": root})
        for index in range(5):
            token = registry.dispatch({**registration(str(index)), "token": root})[
                "token"
            ]
            registry.dispatch(
                {"op": "report", "token": token, "sequence": 1, "status": "completed"}
            )
        rows = registry.snapshots()
        assert {row["runId"] for row in rows} == {"live", "3", "4"}
        assert (
            next(row for row in rows if row["runId"] == "live")["status"] == "running"
        )
    finally:
        registry.close()


def test_late_old_run_completion_does_not_change_new_activation(registry):
    root = registry.capability(Owner())
    old = registry.dispatch({**registration("old"), "token": root})["token"]
    registry.dispatch({**registration("new"), "token": root})
    registry.dispatch(
        {"op": "report", "token": old, "sequence": 10, "status": "completed"}
    )
    newest, oldest = registry.snapshots()
    assert newest["runId"] == "new" and newest["status"] == "running"
    assert oldest["runId"] == "old" and oldest["status"] == "completed"


def test_reporter_reconnects_with_the_same_identity(registry):
    root = registry.capability(Owner("owner"))
    host = Binding(token=root, registry=registry)
    first = host.call(registration())
    assert host.call(registration()) == first
    reporter = Binding(token=str(first["token"]), address=host.endpoint())
    reporter.call({"op": "report", "sequence": 1})
    reporter.call({"op": "report", "sequence": 2, "status": "completed"})
    assert len(registry.snapshots()) == 1
    assert registry.snapshots()[0]["status"] == "completed"


@pytest.mark.parametrize("without_peer_credentials", [False, True])
def test_private_ipc_authenticated_and_closed(
    registry, monkeypatch, without_peer_credentials
):
    if without_peer_credentials:
        monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    token = registry.capability(Owner("owner"))
    host = Binding(token=token, registry=registry)
    address = host.endpoint()
    if not address.startswith("tcp:"):
        assert Path(address).stat().st_mode & 0o777 == 0o600
        assert Path(address).parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(RegistrationError):
        Binding(token="wrong", address=address).call(registration())
    ack = host.call(registration())
    assert ack["token"] != token
    registry.close()
    if not address.startswith("tcp:"):
        assert not Path(address).exists()
    assert not registry._server.thread.is_alive()
    assert not registry._monitor.is_alive()
    with pytest.raises(RegistrationError):
        Binding(token=token, address=address).call(registration())


def test_authenticated_loopback_fallback(registry, monkeypatch):
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    root = registry.capability(Owner("owner"))
    host = Binding(token=root, registry=registry)
    assert host.endpoint().startswith("tcp:")
    with pytest.raises(RegistrationError):
        Binding(token="bad", address=host.endpoint()).call(registration())
    host.call(registration())
    assert registry.snapshots()[0]["sessionID"] == "owner"


def test_unresponsive_host_operation_is_bounded(tmp_path, monkeypatch):
    import yoke.agent_runs.protocol as protocol

    monkeypatch.setattr(protocol, "OPERATION_TIMEOUT", 0.05)
    release = threading.Event()
    accepted = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        address = f"tcp:{server.getsockname()[1]}"
        server.listen()

        def accept():
            with server.accept()[0]:
                accepted.set()
                release.wait(2)

        worker = threading.Thread(target=accept)
        worker.start()
        try:
            with pytest.raises(RegistrationError):
                request(address, {"op": "register", "token": "not-recorded"})
            assert accepted.is_set()
        finally:
            release.set()
            worker.join(timeout=2)
            assert not worker.is_alive()


def test_actual_worker_death_without_heartbeat_timeout(registry):
    root = registry.capability(Owner("host", 77))
    host = Binding(token=root, registry=registry)
    code = """
import os, sys
from yoke.agent_runs.context import current_binding
host = current_binding()
host.call({'op':'register','runId':'crash','agentId':'worker','name':'worker','provider':'fake','model':None})
print('REGISTERED', flush=True)
sys.stdin.readline()
os._exit(7)
"""
    env = {**os.environ, ENVIRONMENT_KEY: host.encode()}
    with subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as worker:
        assert worker.stdout is not None and worker.stdin is not None
        assert worker.stdout.readline() == "REGISTERED\n"
        assert registry.snapshots()[0]["status"] == "running"
        worker.stdin.write("exit\n")
        worker.stdin.flush()
        assert worker.wait(timeout=5) == 7
    registry.reconcile()
    row = registry.snapshots()[0]
    assert row["status"] == "interrupted" and row["observation"] == "lost"
    assert row["finishedAt"] is not None


def test_actual_worker_death_while_hosting_process_is_still_alive(registry):
    root = registry.capability(Owner("host", 77))
    host = Binding(token=root, registry=registry)
    child = """
import os
from yoke.agent_runs.context import current_binding
current_binding().call({'op':'register','runId':'nested-crash','agentId':'nested-worker','name':'worker','provider':'fake','model':None})
os._exit(7)
"""
    outer = f"""
import subprocess, sys
assert subprocess.run([sys.executable, '-c', {child!r}]).returncode == 7
print('CHILD_EXITED', flush=True)
sys.stdin.readline()
"""
    env = {**os.environ, ENVIRONMENT_KEY: host.encode()}
    with subprocess.Popen(
        [sys.executable, "-c", outer],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None and process.stdin is not None
        try:
            assert process.stdout.readline() == "CHILD_EXITED\n"
            assert process.poll() is None
            registry.reconcile()
            assert registry.snapshots()[0]["status"] == "interrupted"
        finally:
            process.stdin.write("exit\n")
            process.stdin.flush()
            process.wait(timeout=5)
