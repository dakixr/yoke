"""Reproductions for admission, retention and process observation failures."""

from __future__ import annotations

import threading


from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.context import Binding
from yoke.agent_runs.records import Owner

from tests.yoke.agent_runs.test_registry import registration


def test_blocked_listener_cannot_block_direct_dispatch_or_reports():
    registry = AgentRunRegistry()
    entered = threading.Event()
    release = threading.Event()
    ack = threading.Event()
    calls = []

    def callback():
        calls.append(threading.get_ident())
        entered.set()
        release.wait(10)

    unsubscribe = registry.subscribe(callback)
    root = registry.capability(Owner())

    def register():
        registry.dispatch({**registration(), "token": root})
        ack.set()

    worker = threading.Thread(target=register)
    worker.start()
    try:
        assert entered.wait(2)
        assert ack.wait(0.5), "Subscriber held the registration ACK"
        host = Binding(token=root, registry=registry)
        token = host.call(registration("other"))["token"]
        reporter = Binding(token=str(token), address=host.endpoint())
        for sequence in range(1, 101):
            reporter.call(
                {"op": "report", "sequence": sequence, "lastToolName": str(sequence)}
            )
        reporter.call({"op": "report", "sequence": 101, "status": "completed"})
        assert registry.snapshots()[0]["status"] == "completed"
        assert len(calls) == 1, "Changes must coalesce while the listener is busy"
        assert calls[0] != worker.ident
    finally:
        unsubscribe()
        release.set()
        worker.join(2)
        registry.close()
        assert registry._notifier._thread is not None
        registry._notifier._thread.join(2)
        assert not registry._notifier._thread.is_alive()


def test_close_does_not_wait_for_blocked_listener():
    registry = AgentRunRegistry()
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def callback():
        entered.set()
        release.wait(10)

    registry.subscribe(callback)
    host = Binding(token=registry.capability(Owner()), registry=registry)
    host.call(registration())
    assert entered.wait(2)

    def close():
        registry.close()
        closed.set()

    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert closed.wait(2), "Registry cleanup waited for subscriber code"
        assert registry.snapshots()[0]["observation"] == "lost"
        assert registry.snapshots()[0]["status"] == "running"
    finally:
        release.set()
        closer.join(3)
        registry.close()
        assert registry._notifier._thread is not None
        registry._notifier._thread.join(2)
        assert not registry._notifier._thread.is_alive()
