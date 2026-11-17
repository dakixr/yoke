"""Explicit command-process phases, including cleanup failures."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from yoke.agent.tools.command_process.phase import (
    IllegalProcessTransition,
    ProcessEvent,
    ProcessPhase,
    next_process_phase,
    process_status,
)
from yoke.agent.tools.command_process.process import _ManagedCommandProcess
from yoke.agent.tools.command_process.manager import CommandProcessManager
from yoke.agent_runs.records import Owner


LEGAL = {
    (ProcessPhase.STARTING, ProcessEvent.START): ProcessPhase.RUNNING,
    (ProcessPhase.STARTING, ProcessEvent.EXIT): ProcessPhase.EXITED,
    (ProcessPhase.STARTING, ProcessEvent.SHUTDOWN): ProcessPhase.EXITING,
    (ProcessPhase.RUNNING, ProcessEvent.EXIT): ProcessPhase.EXITED,
    (ProcessPhase.RUNNING, ProcessEvent.SHUTDOWN): ProcessPhase.EXITING,
    (ProcessPhase.EXITING, ProcessEvent.CLEANUP_COMPLETE): ProcessPhase.EXITED,
    (ProcessPhase.EXITING, ProcessEvent.CLEANUP_FAILED): ProcessPhase.CLEANUP_FAILED,
    (ProcessPhase.EXITED, ProcessEvent.SHUTDOWN): ProcessPhase.EXITED,
    (ProcessPhase.EXITED, ProcessEvent.CLEANUP_COMPLETE): ProcessPhase.EXITED,
    (ProcessPhase.EXITED, ProcessEvent.CLEANUP_FAILED): ProcessPhase.CLEANUP_FAILED,
    (ProcessPhase.CLEANUP_FAILED, ProcessEvent.RETRY): ProcessPhase.EXITING,
}


@pytest.mark.parametrize("phase", list(ProcessPhase), ids=lambda value: value.value)
@pytest.mark.parametrize("event", list(ProcessEvent), ids=lambda value: value.value)
def test_process_phase_table_covers_every_pair(phase, event) -> None:
    if (phase, event) not in LEGAL:
        with pytest.raises(IllegalProcessTransition):
            next_process_phase(phase, event)
        return
    assert next_process_phase(phase, event) is LEGAL[(phase, event)]


@pytest.mark.parametrize(
    ("phase", "exit_code", "status"),
    [
        (ProcessPhase.STARTING, None, "running"),
        (ProcessPhase.RUNNING, None, "running"),
        (ProcessPhase.EXITING, None, "running"),
        (ProcessPhase.EXITED, 0, "exited"),
        (ProcessPhase.EXITED, 2, "failed"),
        (ProcessPhase.EXITED, None, "failed"),
        (ProcessPhase.CLEANUP_FAILED, 0, "failed"),
        (ProcessPhase.CLEANUP_FAILED, None, "failed"),
    ],
)
def test_process_status_projection(phase, exit_code, status) -> None:
    assert process_status(phase, exit_code) == status


class FakeProcess:
    pid = 2_000_000_400

    def __init__(self) -> None:
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self) -> int:
        return 0


def make_process() -> _ManagedCommandProcess:
    return _ManagedCommandProcess(
        session_id=1_400,
        command="TEST-OWNED phase",
        cwd=Path.cwd(),
        process=cast(subprocess.Popen[bytes], FakeProcess()),
        tty=False,
        master_fd=None,
        on_change=lambda: None,
    )


def test_failed_tree_cleanup_is_a_visible_terminal_phase(monkeypatch) -> None:
    managed = make_process()

    def fail_cleanup(*_args: object) -> None:
        raise RuntimeError("injected tree cleanup failure")

    monkeypatch.setattr(
        "yoke.agent.tools.command_process.process.terminate_owned_process_tree",
        fail_cleanup,
    )
    with pytest.raises(RuntimeError, match="injected tree cleanup failure"):
        managed.close()

    assert managed.phase is ProcessPhase.CLEANUP_FAILED
    assert managed.finished is True
    assert managed.closed is True
    assert managed.snapshot().status == "failed"

    monkeypatch.setattr(
        "yoke.agent.tools.command_process.process.terminate_owned_process_tree",
        lambda *_args: None,
    )
    managed.close()
    assert managed.phase is ProcessPhase.EXITED
    assert managed.finished is True
    assert managed.snapshot().status == "exited"


class FailingStream:
    def __init__(self) -> None:
        self.failed = False

    def close(self) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected stream close failure")


def test_failed_local_cleanup_can_be_retried_from_a_defined_phase(monkeypatch) -> None:
    class ProcessWithStreams(FakeProcess):
        def __init__(self) -> None:
            self.stdin = FailingStream()
            self.stdout = FakeStream()
            self.stderr = FakeStream()

    class FakeStream:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "yoke.agent.tools.command_process.process.terminate_owned_process_tree",
        lambda *_args: None,
    )
    managed = _ManagedCommandProcess(
        session_id=1_401,
        command="TEST-OWNED local cleanup",
        cwd=Path.cwd(),
        process=cast(subprocess.Popen[bytes], ProcessWithStreams()),
        tty=False,
        master_fd=None,
        on_change=lambda: None,
    )

    with pytest.raises(RuntimeError, match="injected stream close failure"):
        managed.close()
    assert managed.phase is ProcessPhase.CLEANUP_FAILED
    assert managed.closed is False

    managed.close()
    assert managed.phase is ProcessPhase.EXITED
    assert managed.closed is True


def test_failed_cleanup_keeps_agent_run_authority_open_until_retry() -> None:
    class FakeManagedProcess:
        session_id = 1_002

        def __init__(self) -> None:
            self.fail = True

        def terminate(self) -> None:
            if self.fail:
                raise RuntimeError("injected cleanup failure")

    managed = FakeManagedProcess()
    manager = CommandProcessManager()
    manager._processes = cast(dict[int, _ManagedCommandProcess], {1_002: managed})

    with pytest.raises(RuntimeError, match="injected cleanup failure"):
        manager.close()

    assert manager.agent_runs.capability(Owner("owner"))

    managed.fail = False
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        manager.agent_runs.capability(Owner("owner"))
