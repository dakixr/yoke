"""The full runtime and execution transition products are the lifecycle spec."""

from itertools import product
from typing import get_args

import pytest

from tests.yoke.http.test_runtime_ownership import _execution
from yoke.http.runtime.phase import (
    Activity,
    Idle,
    IllegalTransition,
    OperationActive,
    Retiring,
    RuntimePhase,
    TurnActive,
    next_phase,
    wire_state,
)
from yoke.http.runtime.turns import ExecPhase, SessionOperation, next_exec_phase

RUNTIME_EDGES = {
    (Idle, Idle),
    (Idle, TurnActive),
    (Idle, OperationActive),
    (TurnActive, TurnActive),
    (TurnActive, Retiring),
    (TurnActive, Idle),
    (Retiring, Idle),
    (Retiring, TurnActive),
    (OperationActive, Idle),
    (OperationActive, TurnActive),
}
PHASES = (Idle, TurnActive, Retiring, OperationActive)


@pytest.mark.parametrize(("before", "after"), product(PHASES, repeat=2))
def test_runtime_transition_table(before, after):
    execution = _execution()
    values = {
        Idle: Idle(),
        TurnActive: TurnActive(execution, Activity.THINKING),
        Retiring: Retiring(execution, "user"),
        OperationActive: OperationActive(SessionOperation("op", "selection", "now")),
    }
    if (before, after) in RUNTIME_EDGES:
        assert next_phase(values[before], values[after]) is values[after]
    else:
        with pytest.raises(IllegalTransition):
            next_phase(values[before], values[after])


EXEC_EDGES = {
    (ExecPhase.RESERVED, ExecPhase.WAITING_SLOT),
    (ExecPhase.WAITING_SLOT, ExecPhase.SLOT_HELD),
    (ExecPhase.SLOT_HELD, ExecPhase.WORKER_RUNNING),
    (ExecPhase.WORKER_RUNNING, ExecPhase.EXECUTING),
    *((phase, ExecPhase.DONE) for phase in ExecPhase if phase != ExecPhase.DONE),
}


@pytest.mark.parametrize(("before", "after"), product(ExecPhase, repeat=2))
def test_execution_transition_table(before, after):
    if (before, after) in EXEC_EDGES:
        assert next_exec_phase(before, after) is after
    else:
        with pytest.raises(IllegalTransition):
            next_exec_phase(before, after)


def test_tables_cover_every_state():
    assert set(PHASES) == set(get_args(RuntimePhase.__value__))
    assert {phase.name for phase in ExecPhase} == {
        "RESERVED",
        "WAITING_SLOT",
        "SLOT_HELD",
        "WORKER_RUNNING",
        "EXECUTING",
        "DONE",
    }


def test_activity_projection_and_execution_identity():
    execution = _execution()
    for activity in Activity:
        assert wire_state(TurnActive(execution, activity)) == (
            "waiting_input" if activity == Activity.WAITING_INPUT else "running"
        )
    assert wire_state(Idle()) == "idle"
    assert wire_state(Idle("failure")) == "error"
    assert wire_state(Retiring(execution, "user")) == "stopping"
    assert (
        wire_state(OperationActive(SessionOperation("op", "selection", "now")))
        == "running"
    )
    for target in (
        TurnActive(_execution(), Activity.THINKING),
        Retiring(_execution(), "user"),
    ):
        with pytest.raises(IllegalTransition):
            next_phase(TurnActive(execution, Activity.THINKING), target)
