"""Regression tests for managed command-process ownership and cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import cast

import pytest

from yoke.agent.tools.command_process.process import _ManagedCommandProcess
from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.command_process import lifecycle


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group test")


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
        "yoke.agent.tools.command_process.process.terminate_owned_process_tree",
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
