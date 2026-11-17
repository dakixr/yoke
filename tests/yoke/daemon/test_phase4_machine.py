"""Complete lifecycle table and watcher checks without wall-clock waits."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yoke.daemon import lifecycle as machine
from yoke.daemon.work import DaemonWork, WorkSummary
from yoke.http.runtime.phase import Idle, OperationActive
from yoke.http.runtime.turns import SessionOperation
from yoke.session import SessionStore
from yoke.session.queue import (
    PersistedPendingInput,
    PersistedPromptQueue,
    write_prompt_queue_snapshot,
)


STATES = [
    machine.Serving(),
    machine.Draining(machine.PendingExit.RESTART),
    machine.Draining(machine.PendingExit.STOP),
    machine.Exiting("idle"),
    machine.Exiting("restart"),
    machine.Exiting("stop"),
    machine.Exiting("signal"),
]
# Rows are states above; columns are every event in declaration order.
TABLE = [
    [1, 2, 0, 3, 4, 5, 6],
    [1, 2, 0, None, 4, 5, 6],
    [2, 2, 0, None, None, 5, 6],
    *[[None] * 7 for _ in range(4)],
]


@pytest.mark.parametrize("row", range(len(STATES)))
@pytest.mark.parametrize("event", list(machine.LifecycleEvent))
def test_lifecycle_transition_table(row, event):
    assert len(TABLE[row]) == len(machine.LifecycleEvent)
    target = TABLE[row][list(machine.LifecycleEvent).index(event)]
    if target is None:
        with pytest.raises(machine.IllegalTransition):
            machine.next_phase(STATES[row], event)
    else:
        assert machine.next_phase(STATES[row], event) == STATES[target]


def test_owner_rejects_and_logs_illegal_move(caplog):
    lifecycle = machine.DaemonLifecycle()
    lifecycle.request_stop_when_idle()
    with pytest.raises(machine.IllegalTransition):
        lifecycle.request_exit("restart")
    assert lifecycle.phase == machine.Draining(machine.PendingExit.STOP)
    assert "illegal" in caplog.text.lower()
    lifecycle.cancel_pending_exit()
    assert lifecycle.phase == machine.Serving()
    lifecycle.request_exit("signal")
    lifecycle.request_exit("stop")
    assert lifecycle.exit_reason == "signal"
    with pytest.raises(machine.IllegalTransition):
        lifecycle.begin_mutation()


def test_idle_watcher_fake_clock_and_probe():
    async def scenario():
        now = 0.0
        probes = iter(
            [
                WorkSummary(),
                WorkSummary(running_sessions=("active",)),
                WorkSummary(),
                WorkSummary(),
                WorkSummary(),
            ]
        )
        calls = []

        async def sleep(seconds):
            nonlocal now
            now += seconds

        async def probe():
            calls.append(now)
            return next(probes)

        lifecycle = machine.DaemonLifecycle(
            grace_seconds=2, poll_seconds=1, clock=lambda: now, sleep=sleep
        )
        lifecycle.bind_busy_probe(probe)
        await lifecycle.watch()
        assert calls == [1, 2, 3, 4, 5]
        assert lifecycle.phase == machine.Exiting("idle")

    asyncio.run(scenario())


def test_watcher_observes_cancellation_and_stop_override_during_probe():
    async def scenario():
        async def probe():
            lifecycle.cancel_pending_exit()
            lifecycle.request_stop_when_idle()
            return WorkSummary()

        async def sleep(seconds):
            pass

        lifecycle = machine.DaemonLifecycle(sleep=sleep)
        lifecycle.bind_busy_probe(probe)
        lifecycle.request_restart_when_idle()
        await lifecycle.watch()
        assert lifecycle.phase == machine.Exiting("stop")

    asyncio.run(scenario())


def test_work_summary_counts_phases_workers_and_unheld_queues(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    for session_id in ("runnable", "held", "paused"):
        store.save(session_id, [], root=tmp_path)
        write_prompt_queue_snapshot(
            store.directory,
            session_id,
            PersistedPromptQueue(
                held_reason="interrupt" if session_id == "held" else None,
                prompts=[
                    PersistedPendingInput(
                        id="input",
                        prompt="hello",
                        created_at="now",
                        paused=session_id == "paused",
                    )
                ],
            ),
        )
    runtimes = [
        (
            "selection",
            SimpleNamespace(
                activity=SimpleNamespace(
                    phase=OperationActive(
                        SessionOperation("selection", "selection", "now")
                    )
                ),
                workers=SimpleNamespace(active_count=0),
            ),
        ),
        (
            "cleanup",
            SimpleNamespace(
                activity=SimpleNamespace(phase=Idle()),
                workers=SimpleNamespace(active_count=1),
            ),
        ),
        (
            "error",
            SimpleNamespace(
                activity=SimpleNamespace(phase=Idle(last_error="failed")),
                workers=SimpleNamespace(active_count=0),
            ),
        ),
    ]
    registry = Mock(loaded_runtimes=lambda: runtimes)
    summary = asyncio.run(DaemonWork(store, registry).busy())
    assert summary == WorkSummary(
        running_sessions=("selection", "cleanup"),
        runnable_queues=("runnable",),
        held_queues=("held",),
    )
    assert summary.busy
    assert not WorkSummary(held_queues=("held",)).busy
    assert replace(WorkSummary(), mutations=1).busy


def test_work_summary_marks_completed_admission_as_stale():
    async def scenario():
        lifecycle = machine.DaemonLifecycle()

        async def probe():
            lifecycle.begin_mutation()
            lifecycle.end_mutation()
            return WorkSummary()

        lifecycle.bind_busy_probe(probe)
        summary = await lifecycle.work_summary()
        assert summary.admission_changed
        assert summary.busy
        assert summary.mutations == 0

    asyncio.run(scenario())
