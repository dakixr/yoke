"""The CLI projects daemon authority; it does not guess from render threads."""

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from yoke.cli.backend.session_view import SessionViewStore
from yoke.cli.backend.events import BackendEvent


def changed(
    revision: int, phase: str, input_id: str = "a", **extra: object
) -> BackendEvent:
    return {
        "type": "session.input.changed",
        "payload": {
            "inputID": input_id,
            "inputRevision": revision,
            "phase": phase,
            "prompt": "hello",
            "delivery": "queue",
            "position": 0,
            "clientID": "ours",
            **extra,
        },
    }


@pytest.mark.parametrize(
    "phase,in_queue,running",
    [
        ("queued", True, False),
        ("paused", True, False),
        ("promoted", False, True),
        ("running", False, True),
        ("settled", False, False),
        ("removed", False, False),
    ],
)
@pytest.mark.parametrize(
    "previous", ["queued", "paused", "promoted", "running", "settled", "removed"]
)
def test_input_reducer_transition_table(previous, phase, in_queue, running):
    store = SessionViewStore("s", "ours")
    store.apply_event(changed(1, previous))
    view = store.apply_event(changed(2, phase))
    assert bool(view.queue) == in_queue
    assert bool(view.running) == running
    assert (
        view.inputs.get("a") is None
        if phase in {"settled", "removed"}
        else view.inputs["a"].phase == phase
    )
    assert view.input_revision == 2


def test_gap_duplicates_and_snapshot():
    store = SessionViewStore("s", "ours")
    initial = store.apply_event(changed(1, "queued"))
    assert store.apply_event(changed(1, "running")) == initial
    assert store.apply_event(changed(3, "running")).resync_pending
    view = store.install_snapshot(
        {
            "inputRevision": 3,
            "held": {"reason": "interrupt"},
            "inputs": [
                {"id": "a", "phase": "running", "delivery": "queue", "prompt": "hello"}
            ],
        },
        {"state": "running", "inputID": "a", "revision": 8},
    )
    assert view.running is not None
    assert view.busy and view.running.id == "a" and not view.queue
    assert view.held == "interrupt" and not view.resync_pending
    assert (
        store.apply_event(
            {
                "type": "session.active.changed",
                "payload": {"state": "idle", "revision": 7},
            }
        )
        == view
    )


def test_workspace_paused_local_input_reappears_and_terminal_ids_are_bounded():
    store = SessionViewStore("s", "ours")
    store.mark_local("a")
    store.apply_event(changed(1, "running"))
    paused = store.apply_event(changed(2, "paused", pauseCause="workspace"))
    assert paused.queue[0].origin == "local"
    assert paused.queue[0].pause_cause == "workspace"
    assert not paused.running
    for revision in range(3, 103):
        store.mark_local(str(revision))
        store.apply_event(changed(revision, "settled", str(revision)))
    assert len(store.view.inputs) == 1
    assert not store.owns_input("102")


class ViewMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.store = SessionViewStore("s", "ours")
        self.revision = 0
        self.model = {}
        self.runtime_revision = 0
        self.busy = False

    @rule(
        phase=st.sampled_from(
            ["queued", "paused", "promoted", "running", "settled", "removed"]
        ),
        duplicate=st.booleans(),
        drop=st.booleans(),
    )
    def transition(self, phase, duplicate, drop):
        self.revision += 1
        self.model = {} if phase in {"settled", "removed"} else {"a": phase}
        if not drop:
            event = changed(self.revision, phase)
            view = self.store.apply_event(event)
            if duplicate:
                assert self.store.apply_event(event) == view

    @rule(busy=st.booleans())
    def runtime(self, busy):
        self.busy = busy
        self.runtime_revision += 1
        self.store.apply_event(
            {
                "type": "session.active.changed",
                "payload": {
                    "revision": self.runtime_revision,
                    "state": "running" if busy else "idle",
                },
            }
        )

    @rule()
    def snapshot(self):
        self.store.install_snapshot(
            {
                "inputRevision": self.revision,
                "inputs": [
                    {"id": key, "phase": phase, "delivery": "queue", "prompt": "hello"}
                    for key, phase in self.model.items()
                ],
            },
            {
                "revision": self.runtime_revision,
                "state": "running" if self.busy else "idle",
            },
        )

    @invariant()
    def projection(self):
        view = self.store.view
        if not view.resync_pending and view.input_revision == self.revision:
            assert {
                key: value.phase for key, value in view.inputs.items()
            } == self.model
        assert view.busy == self.busy
        assert not view.running or view.running.id not in {
            item.id for item in view.queue
        }


TestViewMachine = ViewMachine.TestCase
TestViewMachine.settings = settings(
    derandomize=True, max_examples=20, stateful_step_count=25, deadline=500
)


@pytest.mark.parametrize(
    "state,busy",
    [
        ("idle", False),
        ("error", False),
        ("running", True),
        ("stopping", True),
        ("waiting_input", True),
    ],
)
def test_runtime_projection_table(state, busy):
    store = SessionViewStore("s", "ours")
    view = store.apply_event(
        {"type": "session.active.changed", "payload": {"state": state, "revision": 5}}
    )
    assert view.busy == busy
    assert (
        store.apply_event(
            {
                "type": "session.active.changed",
                "payload": {"state": "idle", "revision": 4},
            }
        )
        == view
    )


def test_hold_revision_and_live_legacy_snapshot():
    store = SessionViewStore("s", "ours")
    held = store.apply_event(
        {
            "type": "session.queue.held",
            "payload": {"held": {"reason": "interrupt"}, "inputRevision": 1},
        }
    )
    assert held.held == "interrupt"
    assert (
        store.apply_event(
            {
                "type": "session.queue.held",
                "payload": {"held": None, "inputRevision": 2},
            }
        ).held
        is None
    )
    store.input_lifecycle = False
    view = store.install_legacy_snapshot(
        {"revision": 1, "items": [], "pendingImages": []},
        {"state": "running", "revision": 8},
    )
    assert view.busy
    # A restarted daemon may have a lower, process-local runtime revision.
    assert not store.install_legacy_snapshot(
        {"revision": 1, "items": [], "pendingImages": []},
        {"state": "idle", "revision": 0},
    ).busy
