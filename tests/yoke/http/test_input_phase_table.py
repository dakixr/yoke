"""Every input phase/event pair has an explicit contract."""

import pytest

from yoke.session.input_phase import (
    IllegalTransition,
    InputEvent,
    Queued,
    Paused,
    PauseCause,
    Promoted,
    Running,
    Settled,
    Removed,
    next_phase,
)

STATES = [
    None,
    Queued(0),
    Paused(0, PauseCause.USER),
    Promoted(3, 0),
    Running(3, 0, 7),
    Settled("completed", "now"),
    Removed(),
]
# Independent specification. Keep all rejected pairs in the product below.
EDGES = {
    (None, "admit"): Queued(0),
    (Queued, "pause"): Paused(0, PauseCause.USER),
    (Paused, "resume"): Queued(0),
    (Queued, "edit"): Queued(0),
    (Paused, "edit"): Paused(0, PauseCause.USER),
    (Queued, "move"): Queued(2),
    (Paused, "move"): Paused(2, PauseCause.USER),
    (Queued, "remove"): Removed(),
    (Paused, "remove"): Removed(),
    (Queued, "promote"): Promoted(3, 0),
    (Promoted, "start"): Running(3, 0, 7),
    (Promoted, "block_workspace"): Paused(0, PauseCause.WORKSPACE),
    (Running, "block_workspace"): Paused(0, PauseCause.WORKSPACE),
    (Promoted, "settle"): Settled("completed", "now"),
    (Running, "settle"): Settled("completed", "now"),
    (Promoted, "recover_resume"): Promoted(3, 0),
    (Running, "recover_resume"): Promoted(3, 0),
    (Promoted, "recover_settle"): Settled("recovered", "now"),
    (Running, "recover_settle"): Settled("recovered", "now"),
}


@pytest.mark.parametrize("current", STATES)
@pytest.mark.parametrize("event", list(InputEvent))
def test_complete_input_transition_table(current, event):
    target = EDGES.get((type(current) if current else None, event.value))
    if target is None:
        for candidate in STATES[1:]:
            with pytest.raises(IllegalTransition):
                assert candidate is not None
                next_phase(current, event, candidate)
    else:
        assert next_phase(current, event, target) == target
        for candidate in STATES[1:]:
            if type(candidate) is not type(target):
                assert candidate is not None
                with pytest.raises(IllegalTransition):
                    next_phase(current, event, candidate)


def test_workspace_pause_repair_is_idempotent_and_keeps_its_cause():
    phase = Paused(0, PauseCause.WORKSPACE)
    assert next_phase(phase, InputEvent.BLOCK_WORKSPACE, phase) == phase
    with pytest.raises(IllegalTransition):
        next_phase(
            Promoted(1, 0), InputEvent.BLOCK_WORKSPACE, Paused(0, PauseCause.USER)
        )
    with pytest.raises(IllegalTransition):
        next_phase(Queued(0), InputEvent.PAUSE, phase)
