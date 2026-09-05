"""Regression tests for managed command-process ownership and cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any
from typing import cast

import pytest

from yoke.agent.tools.command_process import _ManagedCommandProcess
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_support import lifecycle


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group test")


def _wait_until(predicate: Any, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        wake.wait(0.01)
    return bool(predicate())


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text(encoding="utf-8").split()[2] != "Z"
        except (FileNotFoundError, IndexError):
            return False
    return True


def _reap_test_group(manager: CommandProcessManager, group_id: int | None) -> None:
    group_ids = {process.process.pid for process in manager._processes.values()}
    if group_id is not None:
        group_ids.add(group_id)
    for owned_group_id in group_ids:
        try:
            os.killpg(owned_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        manager.close()
    except BaseException:
        pass


@posix_only
def test_terminate_kills_group_after_leader_exits_with_pipe_held(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_ready_path = tmp_path / "child.ready"
    child_code = (
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "Path(sys.argv[2]).write_text('ready'); "
        "print('child-ready', flush=True); time.sleep(60)"
    )
    leader_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
        "sys.argv[2], sys.argv[3]])"
    )
    manager = CommandProcessManager()
    group_id: int | None = None
    child_pid: int | None = None
    try:
        result = manager.exec_argv(
            argv=[
                sys.executable,
                "-c",
                leader_code,
                child_code,
                str(child_pid_path),
                str(child_ready_path),
            ],
            display_command="TEST-OWNED leader-exited pipe holder",
            cwd=tmp_path,
            env=os.environ.copy(),
            yield_time_ms=250,
            timeout_seconds=None,
            cancel_requested=None,
        )
        assert result.session_id is not None
        managed = manager._processes[result.session_id]
        group_id = managed.process.pid
        managed.process.wait(timeout=5)
        assert _wait_until(child_ready_path.exists)
        child_pid = _read_pid(child_pid_path)
        assert child_pid is not None and _pid_is_live(child_pid)
        assert manager.snapshot(result.session_id).status == "running"

        started = time.monotonic()
        manager.terminate(result.session_id)

        assert time.monotonic() - started < 3
        snapshot = manager.snapshot(result.session_id)
        assert snapshot.status == "exited"
        assert snapshot.exit_code == 0
        assert snapshot.output_tail == "child-ready\n"
        assert _wait_until(lambda: not _pid_is_live(child_pid))
    finally:
        _reap_test_group(manager, group_id)


@posix_only
def test_timeout_kills_term_ignoring_parent_and_child(tmp_path: Path) -> None:
    leader_pid_path = tmp_path / "leader.pid"
    child_pid_path = tmp_path / "child.pid"
    ready_path = tmp_path / "tree.ready"
    child_code = (
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "Path(sys.argv[2]).write_text('ready'); "
        "print('tree-ready', flush=True); time.sleep(60)"
    )
    leader_code = (
        "import os,signal,subprocess,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[2], "
        "sys.argv[3]]); time.sleep(60)"
    )
    manager = CommandProcessManager()
    group_id: int | None = None
    try:
        started = time.monotonic()
        result = manager.exec_argv(
            argv=[
                sys.executable,
                "-c",
                leader_code,
                str(leader_pid_path),
                str(child_pid_path),
                str(ready_path),
                child_code,
            ],
            display_command="TEST-OWNED timeout process tree",
            cwd=tmp_path,
            env=os.environ.copy(),
            yield_time_ms=5_000,
            timeout_seconds=1,
            cancel_requested=None,
        )
        group_id = _read_pid(leader_pid_path)
        child_pid = _read_pid(child_pid_path)

        assert ready_path.exists()
        assert result.session_id is None
        assert result.timed_out is True
        assert result.exit_code is not None
        assert result.output == "tree-ready\n"
        assert manager.snapshots()[0].status == "failed"
        assert time.monotonic() - started < 4
        assert group_id is not None and _wait_until(lambda: not _pid_is_live(group_id))
        assert child_pid is not None and _wait_until(
            lambda: not _pid_is_live(child_pid)
        )
    finally:
        _reap_test_group(manager, group_id or _read_pid(leader_pid_path))


@posix_only
def test_cancelled_wait_keeps_test_process_managed(tmp_path: Path) -> None:
    ready_path = tmp_path / "cancel.ready"
    code = (
        "import os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    cancelled = threading.Event()
    cancelled.set()
    manager = CommandProcessManager()
    group_id: int | None = None
    try:
        result = manager.exec_argv(
            argv=[sys.executable, "-c", code, str(ready_path)],
            display_command="TEST-OWNED cancellation background process",
            cwd=tmp_path,
            env=os.environ.copy(),
            yield_time_ms=5_000,
            timeout_seconds=None,
            cancel_requested=cancelled.is_set,
        )
        assert result.session_id is not None
        group_id = manager.snapshot(result.session_id).pid
        assert manager.snapshot(result.session_id).status == "running"
        assert result.session_id in manager._processes
    finally:
        _reap_test_group(manager, group_id)


def test_terminate_all_attempts_every_process_and_close_retries() -> None:
    class FakeManagedProcess:
        def __init__(self, session_id: int, *, fail: bool) -> None:
            self.session_id = session_id
            self.fail = fail
            self.calls = 0

        def terminate(self) -> None:
            self.calls += 1
            if self.fail:
                raise RuntimeError(f"injected cleanup failure {self.session_id}")

    first = FakeManagedProcess(1_001, fail=True)
    second = FakeManagedProcess(1_002, fail=False)
    manager = CommandProcessManager()
    manager._processes = cast(
        dict[int, _ManagedCommandProcess],
        {first.session_id: first, second.session_id: second},
    )

    with pytest.raises(RuntimeError, match="injected cleanup failure 1001"):
        manager.close()

    assert first.calls == 1
    assert second.calls == 1
    assert list(manager._processes) == [first.session_id]

    first.fail = False
    manager.close()
    assert first.calls == 2
    assert manager._processes == {}


def test_local_close_attempts_every_descriptor_and_can_retry(monkeypatch) -> None:
    calls: list[str] = []

    class Stream:
        def __init__(self, name: str, *, fail_once: bool = False) -> None:
            self.name = name
            self.fail_once = fail_once

        def close(self) -> None:
            calls.append(self.name)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("injected stream close failure")

    class FinishedProcess:
        pid = 2_000_000_000
        stdin = Stream("stdin", fail_once=True)
        stdout = Stream("stdout")
        stderr = Stream("stderr")

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "yoke.agent.tools.command_process.terminate_owned_process_tree",
        lambda *_args: None,
    )
    managed = _ManagedCommandProcess(
        session_id=1_003,
        command="TEST-OWNED fake close",
        cwd=Path.cwd(),
        process=cast(subprocess.Popen[bytes], FinishedProcess()),
        tty=False,
        master_fd=None,
        on_change=lambda: None,
    )

    with pytest.raises(RuntimeError, match="injected stream close failure"):
        managed.close()
    assert calls == ["stdin", "stdout", "stderr"]

    managed.close()
    assert calls == ["stdin", "stdout", "stderr", "stdin", "stdout", "stderr"]
    assert managed.closed is True


def test_windows_taskkill_nonzero_uses_process_kill_fallback(monkeypatch) -> None:
    calls: list[str] = []

    class FakeProcess:
        pid = 12_345

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            calls.append(f"wait:{timeout}")
            return 1

    monkeypatch.setattr(lifecycle.shutil, "which", lambda _name: "taskkill.exe")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )

    lifecycle._terminate_windows_process_tree(
        cast(subprocess.Popen[bytes], FakeProcess())
    )

    assert calls == ["kill", f"wait:{lifecycle.KILL_WAIT_SECONDS}"]


def test_final_lease_release_drains_live_readers(tmp_path: Path) -> None:
    manager = CommandProcessManager().acquire()
    manager.acquire()
    process = manager._spawn(
        "sleep",
        tmp_path,
        False,
        None,
        False,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        manager.release()
        assert process.process.poll() is None
        manager.release()
        assert process.process.poll() is not None
        assert all(not thread.is_alive() for thread in process._reader_threads)
        with pytest.raises(RuntimeError, match="closed"):
            manager.acquire()
    finally:
        manager.close()


def test_capacity_pruning_drains_readers_before_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.agent.tools.command_process_support import admission

    monkeypatch.setattr(admission, "MAX_PROCESS_COUNT", 1)
    manager = CommandProcessManager()
    try:
        first = manager._spawn(
            "first",
            tmp_path,
            False,
            None,
            False,
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        second = manager._spawn(
            "second",
            tmp_path,
            False,
            None,
            False,
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        assert first.process.poll() is not None
        assert all(not thread.is_alive() for thread in first._reader_threads)
        assert second.process.poll() is None
        assert len(manager.snapshots()) == 1
    finally:
        manager.close()


def test_final_release_clears_completed_output(tmp_path: Path) -> None:
    manager = CommandProcessManager().acquire()
    try:
        process = manager._spawn(
            "done",
            tmp_path,
            False,
            None,
            False,
            argv=[sys.executable, "-c", "print('retained')"],
        )
        assert _wait_until(lambda: process.finished)
        manager._complete(process.session_id)
        assert manager.snapshots()
        manager.release()
        assert manager.snapshots() == []
        manager.close()
    finally:
        manager.close()
