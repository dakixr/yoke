"""Reproductions for admission, retention and process observation failures."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.context import Binding, bind_binding
from yoke.agent_runs.protocol import RegistrationError
from yoke.agent_runs.records import Owner
from yoke.agent_runs.reporting import Reporter

from tests.yoke.agent_runs.test_launches import CHILD_CODE
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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd required")
def test_high_pidfd_death_does_not_abort_other_reconciliation(monkeypatch):
    import ctypes
    import fcntl
    import resource
    import select

    if not hasattr(os, "pidfd_open"):
        libc = ctypes.CDLL(None, use_errno=True)

        def pidfd_open(pid):
            fd = libc.pidfd_open(pid, 0)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "pidfd_open")
            return fd

        monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
    now = [0.0]
    registry = AgentRunRegistry(clock=lambda: now[0], stale_after=5)
    root = registry.capability(Owner())
    original_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(
        resource.RLIMIT_NOFILE, (max(original_limit[0], 2048), original_limit[1])
    )
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        registry.dispatch({**registration("dead"), "token": root}, pid=process.pid)
        registry.dispatch({**registration("alive"), "token": root})
        with registry._lock:
            worker = registry._records["dead"].worker
            assert worker.fd is not None
            high_fd = fcntl.fcntl(worker.fd, fcntl.F_DUPFD_CLOEXEC, 1024)
            os.close(worker.fd)
            worker.fd = high_fd
            with pytest.raises(ValueError, match="out of range"):
                select.select([high_fd], [], [], 0)
            process.terminate()
            process.wait(timeout=5)
            now[0] = 6
            registry.reconcile()
        rows = {row["runId"]: row for row in registry.snapshots()}
        assert rows["dead"]["status"] == "interrupted"
        assert rows["dead"]["observation"] == "lost"
        assert rows["alive"]["status"] == "running"
        assert rows["alive"]["observation"] == "stale"
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        registry.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, original_limit)


@pytest.mark.parametrize("local_binding", [False, True])
def test_persistent_child_prompts_after_launching_parent_is_evicted(
    tmp_path, local_binding
):
    manager = CommandProcessManager()
    manager.agent_runs.close()
    manager.agent_runs = AgentRunRegistry(history_limit=1)
    registry = manager.agent_runs
    host = Binding(token=registry.capability(Owner("owner", 77)), registry=registry)
    parent = Reporter(host, registration("parent", "parent-agent"))
    binding = parent.binding
    if local_binding:
        binding = Binding(token=binding.token, registry=registry)
    code = (
        CHILD_CODE
        + "\nprint('FIRST_DONE', flush=True)\nimport sys; sys.stdin.readline()\n"
        + CHILD_CODE
        + "\nprint('SECOND_DONE', flush=True)\n"
    )
    try:
        with bind_binding(binding):
            managed = manager._spawn(
                "persistent-child",
                tmp_path,
                False,
                None,
                False,
                argv=[sys.executable, "-c", code],
            )
        with managed.condition:
            assert managed.condition.wait_for(
                lambda: (
                    "FIRST_DONE" in manager.snapshot(managed.session_id).output_tail
                ),
                timeout=10,
            )
        parent.finish("completed")
        filler = Reporter(host, registration("filler", "other-agent"))
        filler.finish("completed")
        assert {row["runId"] for row in registry.snapshots()} == {"filler"}
        manager.write_input(managed.session_id, "again\n")
        assert managed.process.wait(timeout=10) == 0
        with managed.condition:
            assert managed.condition.wait_for(lambda: managed.finished, timeout=10)
        assert "SECOND_DONE" in manager.snapshot(managed.session_id).output_tail
        row = registry.snapshots()[0]
        assert row["status"] == "completed"
        assert row["parentRunId"] == "parent"
        assert row["parentAgentId"] == "parent-agent"
        assert row["sessionID"] == "owner"
        assert row["runtimeSessionID"] == 77
    finally:
        manager.close()


def test_forwarded_launch_capability_is_revoked_on_manager_cleanup(tmp_path):
    registry = AgentRunRegistry()
    host = Binding(token=registry.capability(Owner("owner", 1)), registry=registry)
    manager = CommandProcessManager()
    before = set(registry._capabilities)
    try:
        with bind_binding(Binding(token=host.token, address=host.endpoint())):
            managed = manager._spawn(
                "child",
                tmp_path,
                False,
                None,
                False,
                argv=[sys.executable, "-c", "pass"],
            )
        assert managed.process.wait(timeout=5) == 0
        tokens = set(registry._capabilities) - before
        assert len(tokens) == 1
        token = tokens.pop()
        manager.close()
        assert token not in registry._capabilities
        with pytest.raises(RegistrationError):
            Binding(token=token, address=host.endpoint()).call(registration())
    finally:
        manager.close()
        registry.close()
