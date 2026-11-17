"""Durable queue holds let an idle daemon stop after a failed turn."""

import asyncio

import pytest

from tests.yoke.http.test_runtime_ownership import _execution, _runtime
from yoke.daemon.work import DaemonWork
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.session.queue import has_queued_prompts


@pytest.mark.parametrize("boundary", ["stop", "finalization", "selection", "restart"])
def test_b1_stranded_queue_is_held_and_does_not_count_as_work(tmp_path, boundary):
    registry, runtime = _runtime(tmp_path)
    pending = runtime.pending_inputs
    pending.admit(
        "owned",
        PromptAdmissionRequest.model_validate(
            {
                "id": "queued",
                "prompt": {"text": "later"},
                "resume": False,
            }
        ),
    )

    async def scenario():
        if boundary == "restart":
            pending.hold_stranded()
        elif boundary == "selection":
            from yoke.http.runtime.turns import SessionOperation
            from yoke.http.runtime.phase import OperationActive

            operation = SessionOperation(
                id="selection", kind="selection", started_at="now"
            )
            runtime.activity.enter(OperationActive(operation), cause="test")
            with pytest.raises(RuntimeError):
                with runtime.activity.operating(operation):
                    raise RuntimeError("selection failed")
        else:
            execution = _execution()
            runtime.activity.active = execution
            if boundary == "stop":
                await runtime.interrupt()
            else:
                await runtime.finalizer.fail(execution, OSError("finalization failed"))
        assert not has_queued_prompts(runtime.store.directory)
        assert not await DaemonWork(runtime.store, registry).busy()
        queue = pending.queue("owned")
        assert [item.id for item in queue.items] == ["queued"]
        assert (
            queue.held.reason
            == {
                "stop": "interrupt",
                "finalization": "finalization_failed",
                "selection": "selection_failed",
                "restart": "daemon_restart",
            }[boundary]
        )

    try:
        asyncio.run(scenario())
    finally:
        registry.shutdown_executor()


def test_input_transition_rolls_back_phase_queue_revision_and_event_together(
    tmp_path, monkeypatch
):
    from yoke.session.input_phase import InputEvent, Running
    from yoke.session.queue import prompt_queue_transaction

    registry, runtime = _runtime(tmp_path)
    pending = runtime.pending_inputs
    before = pending.control.snapshot("owned", live=False)
    before_events = pending.events.journal.latest_sequence("owned")
    original = pending.events.durable

    def fail(session_id, event_type, *args, **kwargs):
        if event_type == "session.input.changed":
            raise OSError("transition journal failed")
        return original(session_id, event_type, *args, **kwargs)

    monkeypatch.setattr(pending.events, "durable", fail)
    try:
        record = pending.get_input("owned", "input")
        assert record.promoted_seq is not None
        with pytest.raises(OSError, match="transition journal failed"):
            with prompt_queue_transaction(runtime.store.directory, "owned") as tx:
                pending.lifecycle.apply(
                    tx,
                    "owned",
                    record,
                    InputEvent.START,
                    Running(record.promoted_seq, 0, 7),
                )
        assert pending.control.snapshot("owned", live=False) == before
        assert pending.events.journal.latest_sequence("owned") == before_events
    finally:
        registry.shutdown_executor()


def test_only_one_promoted_input_can_own_the_session(tmp_path):
    from yoke.session.input_phase import IllegalTransition

    registry, runtime = _runtime(tmp_path)
    pending = runtime.pending_inputs
    pending.admit(
        "owned",
        PromptAdmissionRequest.model_validate(
            {
                "id": "second",
                "prompt": {"text": "second"},
                "resume": False,
            }
        ),
    )
    before = pending.queue("owned")
    try:
        with pytest.raises(IllegalTransition, match="Another input"):
            pending.pop_next("owned", allow_queue=True)
        assert pending.queue("owned") == before
    finally:
        registry.shutdown_executor()


def test_daemon_startup_holds_stranded_queue_before_serving(tmp_path):
    from tests.yoke.http.test_input_lifecycle_stateful import service
    from yoke.daemon.lifecycle import DaemonLifecycle
    from yoke.daemon.server import _build_server

    pending = service(tmp_path / "sessions")
    pending.admit(
        "inputs",
        PromptAdmissionRequest.model_validate(
            {
                "id": "stranded",
                "prompt": {"text": "after restart"},
                "resume": False,
            }
        ),
    )
    assert has_queued_prompts(pending.store.directory)
    server = _build_server(
        token="test",
        lifecycle=DaemonLifecycle(idle_exit=False),
        socket_path=tmp_path / "unused.sock",
        port=0,
        verbose=False,
        session_directory=pending.store.directory,
    )
    try:
        assert not has_queued_prompts(pending.store.directory)
        held = pending.queue("inputs").held
        assert held is not None
        assert held.reason == "daemon_restart"
        assert pending.get_input("inputs", "stranded").phase.kind == "queued"
    finally:
        from fastapi import FastAPI

        assert isinstance(server.config.app, FastAPI)
        server.config.app.state.runtime_registry.shutdown_executor()
