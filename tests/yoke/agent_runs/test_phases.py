"""Explicit agent-run lifecycles and lost-terminal retention."""

from __future__ import annotations

import pytest

from yoke.agent_runs.phases import (
    IllegalRunTransition,
    RunEvent,
    RunPhase,
    is_terminal,
    next_run_phase,
    public_observation,
)
from yoke.agent_runs.records import Owner
from yoke.agent_runs.registry import AgentRunRegistry

LEGAL = {
    (RunPhase.RUNNING, RunEvent.HEARTBEAT): RunPhase.RUNNING,
    (RunPhase.RUNNING, RunEvent.STALE): RunPhase.RUNNING,
    (RunPhase.RUNNING, RunEvent.LOST): RunPhase.LOST,
    (RunPhase.RUNNING, RunEvent.COMPLETED): RunPhase.COMPLETED,
    (RunPhase.RUNNING, RunEvent.FAILED): RunPhase.FAILED,
    (RunPhase.RUNNING, RunEvent.CANCELLED): RunPhase.CANCELLED,
    (RunPhase.RUNNING, RunEvent.INTERRUPTED): RunPhase.INTERRUPTED,
    (RunPhase.LOST, RunEvent.HEARTBEAT): RunPhase.RUNNING,
    (RunPhase.LOST, RunEvent.LOST): RunPhase.LOST,
    (RunPhase.LOST, RunEvent.COMPLETED): RunPhase.COMPLETED,
    (RunPhase.LOST, RunEvent.FAILED): RunPhase.FAILED,
    (RunPhase.LOST, RunEvent.CANCELLED): RunPhase.CANCELLED,
    (RunPhase.LOST, RunEvent.INTERRUPTED): RunPhase.INTERRUPTED,
}


@pytest.mark.parametrize("phase", list(RunPhase), ids=lambda value: value.value)
@pytest.mark.parametrize("event", list(RunEvent), ids=lambda value: value.value)
def test_run_phase_table_covers_every_pair(phase, event) -> None:
    if (phase, event) not in LEGAL:
        with pytest.raises(IllegalRunTransition):
            next_run_phase(phase, event)
        return
    assert next_run_phase(phase, event) is LEGAL[(phase, event)]


def test_lost_is_terminal_for_retention_but_not_for_the_wire_status() -> None:
    assert is_terminal(RunPhase.LOST)
    assert public_observation(RunPhase.LOST, "live") == "lost"
    assert public_observation(RunPhase.RUNNING, "stale") == "stale"


def registration(run: str) -> dict[str, object]:
    return {
        "op": "register",
        "runId": run,
        "agentId": "agent",
        "name": "reviewer",
        "provider": "fixture",
        "model": "test",
    }


def test_lost_run_is_evicted_after_the_retention_window() -> None:
    now = [0.0]
    registry = AgentRunRegistry(
        history_limit=1,
        stale_after=5,
        lost_after=10,
        clock=lambda: now[0],
    )
    root = registry.capability(Owner("owner"))
    try:
        registry.dispatch({**registration("lost"), "token": root})
        now[0] = 11
        registry.reconcile()

        row = registry.snapshots()[0]
        assert row["runId"] == "lost"
        assert row["status"] == "running" and row["observation"] == "lost"
        assert row["finishedAt"] is None

        token = registry.dispatch({**registration("done"), "token": root})["token"]
        registry.dispatch(
            {"op": "report", "token": token, "sequence": 1, "status": "completed"}
        )
        now[0] = 12
        registry.reconcile()

        assert [item["runId"] for item in registry.snapshots()] == ["done"]
    finally:
        registry.close()


def test_a_lost_run_that_has_not_been_evicted_revives_on_a_heartbeat() -> None:
    now = [0.0]
    registry = AgentRunRegistry(
        history_limit=1,
        stale_after=5,
        lost_after=10,
        clock=lambda: now[0],
    )
    root = registry.capability(Owner("owner"))
    try:
        token = registry.dispatch({**registration("run"), "token": root})["token"]
        now[0] = 11
        registry.reconcile()
        assert registry.snapshots()[0]["observation"] == "lost"

        registry.dispatch({"op": "report", "token": token, "sequence": 2})
        row = registry.snapshots()[0]
        assert row["status"] == "running"
        assert row["observation"] == "live"
        assert row["finishedAt"] is None
    finally:
        registry.close()
