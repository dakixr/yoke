"""Run state, heartbeat revisions, capability ownership and retention."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import threading

import pytest

from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.context import Binding
from yoke.agent_runs.protocol import RegistrationError, request
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


@pytest.mark.parametrize(
    "usage",
    [
        {"totalTokens": -1},
        {"totalTokens": True},
        {"totalTokens": "12"},
        {"durationMs": float("nan")},
        {"totalTokens": 2**53},
    ],
)
def test_invalid_usage_cannot_change_a_run(registry, usage):
    root = registry.capability(Owner("owner"))
    token = registry.dispatch({**registration(), "token": root})["token"]
    before = registry.snapshots()[0]
    with pytest.raises(ValueError, match="Invalid agent run usage"):
        registry.dispatch(
            {"op": "report", "token": token, "sequence": 1, "typedUsage": usage}
        )
    assert registry.snapshots()[0] == before
    registry.dispatch(
        {
            "op": "report",
            "token": token,
            "sequence": 1,
            "typedUsage": {"totalTokens": 12, "prompt": "SECRET"},
        }
    )
    assert registry.snapshots()[0]["typedUsage"] == {"totalTokens": 12}


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


def test_close_interrupts_an_idle_listener(registry):
    from yoke.agent_runs.server import Server

    ready = threading.Event()

    class LongAcceptServer(Server):
        def _serve(self):
            self.socket.settimeout(60)
            ready.set()
            super()._serve()

    server = LongAcceptServer(registry)
    try:
        # The listener uses a long timeout before entering its accept loop.
        assert ready.wait(5)
        server.close()
        assert not server.thread.is_alive()
    finally:
        server.close()
