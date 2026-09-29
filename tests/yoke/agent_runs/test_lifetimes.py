"""Host cleanup when remote managers and captured SDK instances crash."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time

import pytest

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent_runs import AgentRunRegistry

from tests.yoke.agent_runs.test_launches import CHILD_CODE


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        assert time.monotonic() < deadline, f"Timed out waiting for {path}"
        time.sleep(0.01)


def test_host_retires_remote_launches_after_repeated_issuer_crashes(tmp_path):
    manager = CommandProcessManager()
    registry = manager.agent_runs
    code = """
import os, sys
from pathlib import Path
from yoke.agent.tools.command_process_manager import CommandProcessManager
manager = CommandProcessManager()
child = manager._spawn('nested', Path.cwd(), False, None, False,
    argv=[sys.executable, '-c', 'pass'])
assert child.process.wait(timeout=10) == 0
print('NESTED_FINISHED', flush=True)
sys.stdin.readline()
os._exit(7)
"""
    try:
        baseline = set(registry._capabilities)
        for _ in range(3):
            managed = manager._spawn(
                "outer",
                tmp_path,
                False,
                None,
                False,
                argv=[sys.executable, "-c", code],
            )
            with managed.condition:
                assert managed.condition.wait_for(
                    lambda: (
                        "NESTED_FINISHED"
                        in manager.snapshot(managed.session_id).output_tail
                    ),
                    timeout=15,
                )
            derived = {
                token: cap.lifetime
                for token, cap in registry._capabilities.items()
                if cap.lifetime is not None and cap.lifetime.worker is not None
            }
            assert len(derived) == 1
            manager.write_input(managed.session_id, "exit\n")
            assert managed.process.wait(timeout=10) == 7
            with managed.condition:
                assert managed.condition.wait_for(lambda: managed.finished, timeout=5)
            manager._complete(managed.session_id)
            registry.reconcile()
            if os.name == "nt":
                # Windows handles prove issuer/child exit, not every unobserved
                # descendant's exit. Explicit release completes that cleanup.
                for token in derived:
                    assert token in registry._capabilities
                    registry.revoke(token)
            assert set(registry._capabilities) == baseline
            for lifetime in derived.values():
                assert lifetime.worker is not None
                assert lifetime.worker.fd is None
                assert lifetime.worker._handle is None
                assert lifetime.worker._queue is None
    finally:
        manager.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group descendants")
@pytest.mark.parametrize("lost_attach", [False, True])
def test_remote_launcher_and_issuer_death_preserve_live_unregistered_descendant(
    tmp_path,
    lost_attach,
):
    manager = CommandProcessManager()
    registry = manager.agent_runs
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    done = tmp_path / "done"
    grandchild = f"""
import os, time
from pathlib import Path
Path({str(ready)!r}).write_text(str(os.getpgrp()))
deadline = time.monotonic() + 20
while not Path({str(release)!r}).exists():
    if time.monotonic() > deadline:
        os._exit(9)
    time.sleep(0.01)
{CHILD_CODE}
Path({str(done)!r}).touch()
os._exit(0)
"""
    launcher = f"""
import subprocess, sys
subprocess.Popen([sys.executable, '-c', {grandchild!r}])
"""
    outer = f"""
import os, sys
from pathlib import Path
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent_runs.context import Binding
from yoke.agent_runs.protocol import RegistrationError
if {lost_attach!r}:
    def fail_attach(self, pid):
        raise RegistrationError('attachment request lost before dispatch')
    Binding.attach = fail_attach
manager = CommandProcessManager()
child = manager._spawn('launcher', Path.cwd(), False, None, False,
    argv=[sys.executable, '-c', {launcher!r}])
assert child.process.wait(timeout=10) == 0
os._exit(7)
"""
    group = None
    try:
        managed = manager._spawn(
            "issuer",
            tmp_path,
            False,
            None,
            False,
            argv=[sys.executable, "-c", outer],
        )
        wait_for(ready)
        assert managed.process.wait(timeout=10) == 7
        with managed.condition:
            assert managed.condition.wait_for(lambda: managed.finished, timeout=5)
        remote = [
            (token, cap.lifetime)
            for token, cap in registry._capabilities.items()
            if cap.lifetime is not None
            and cap.lifetime.process_launch
            and cap.lifetime.worker is not None
        ]
        assert len(remote) == 1
        token, lifetime = remote[0]
        group = int(ready.read_text())
        manager._complete(managed.session_id)
        # Repeat to catch consumed one-shot process death notifications as well.
        for _ in range(2):
            registry.reconcile()
            assert token in registry._capabilities
        release.touch()
        wait_for(done)
        assert registry.snapshots()[0]["status"] == "completed"
        deadline = time.monotonic() + 10
        while token in registry._capabilities:
            assert time.monotonic() < deadline
            registry.reconcile()
            time.sleep(0.01)
    finally:
        release.touch()
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        manager.close()


def test_host_releases_captured_instance_authority_after_worker_crash(tmp_path):
    manager = CommandProcessManager()
    manager.agent_runs.close()
    manager.agent_runs = AgentRunRegistry(history_limit=0)
    registry = manager.agent_runs
    code = (
        CHILD_CODE.split("try:\n")[0]
        + "\nimport os, sys\nprint('CAPTURED', flush=True)\nsys.stdin.readline()\nos._exit(7)\n"
    )
    try:
        managed = manager._spawn(
            "instance",
            tmp_path,
            False,
            None,
            False,
            argv=[sys.executable, "-c", code],
        )
        with managed.condition:
            assert managed.condition.wait_for(
                lambda: "CAPTURED" in manager.snapshot(managed.session_id).output_tail,
                timeout=15,
            )
        assert len(registry._capabilities) == 2
        assert registry.snapshots() == []
        manager.write_input(managed.session_id, "exit\n")
        assert managed.process.wait(timeout=10) == 7
        with managed.condition:
            assert managed.condition.wait_for(lambda: managed.finished, timeout=5)
        manager._complete(managed.session_id)
        registry.reconcile()
        assert registry._capabilities == {}
    finally:
        manager.close()
