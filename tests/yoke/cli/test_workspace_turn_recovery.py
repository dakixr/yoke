"""Deletion races between prompt ownership transfer and model execution."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
from unittest.mock import Mock

import pytest

from yoke.agent.models import Message
from yoke.agent.state import conversation_entries_from_messages
from yoke.cli.interactive.common import PendingPrompt, PromptCliState, TurnFailure
from yoke.cli.interactive.prompt import turns
from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.interactive.queue.mutations import append_prompt, dequeue_prompt
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.session.queue import load_prompt_queue_snapshot
from yoke.session.workspace import WorkspaceUnavailable, require_workspace

from .support import FakeAgent, active_session_for
from .test_queue_action_atomicity import _attach, _tiny_png


def _state() -> PromptCliState:
    return PromptCliState(
        messages=[],
        pending_prompts=[],
        active_turn_id=1,
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )


def _run_and_finish(active, state, lock, agent, prompt, user_message):
    outcomes = []
    turns.run_prompt_turn(
        turn_id=state.active_turn_id,
        prompt=prompt,
        state=state,
        state_lock=lock,
        agent=agent,
        active_session=active,
        stop_event=Event(),
        user_message=user_message,
        callbacks={"handle_outcome": lambda _id, outcome: outcomes.append(outcome)},
        turn_renderer_factory=Mock(),
    )
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, TurnFailure)
    assert isinstance(outcome.error, WorkspaceUnavailable)
    assert (
        turns.handle_prompt_turn_outcome(
            turn_id=state.active_turn_id,
            outcome=outcome,
            state=state,
            state_lock=lock,
            agent=agent,
            active_session=active,
            renderer=Mock(),
            scrollback=Mock(),
        )
        is False
    )
    next_prompt, finish = turns.finish_prompt_turn(
        state=state,
        state_lock=lock,
        active_session=active,
        request_context_usage=lambda _text: None,
    )
    assert next_prompt is None
    assert not finish
    assert state.active_user_message is None
    return outcome


@pytest.mark.parametrize("kind", ["queued", "steering"])
def test_dequeue_delete_run_outcome_finish_restores_exact_paused_item(
    tmp_path: Path, kind
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    state, lock, agent = _state(), Lock(), FakeAgent()
    first = PendingPrompt("earlier", paused=True)
    image = _tiny_png(tmp_path / "image.png")
    from yoke.cli.image_input import build_user_message

    selected = PendingPrompt(
        "describe it",
        kind=kind,
        user_message=build_user_message("describe it", image_paths=[image.path]),
        attachments=[{"type": "image", "url": "data:image/png;base64,abc"}],
    )
    later = PendingPrompt("later", paused=True)
    for item in [first, selected, later]:
        assert append_prompt(
            state=state, state_lock=lock, active_session=active, prompt=item
        )
    pending = dequeue_prompt(state=state, state_lock=lock, active_session=active)
    assert pending is not None
    assert pending == selected
    assert load_prompt_queue_state(active).prompts == [first, later]
    # Deletion is deliberately after durable removal, not before dequeue.
    root.rmdir()
    image.path.unlink()
    outcome = _run_and_finish(
        active, state, lock, agent, pending.prompt, pending.user_message
    )
    assert outcome.rejected_prompt is not None
    assert outcome.rejected_prompt[0] is pending
    assert outcome.rejected_prompt[1] == 1
    expected = selected.copy_for_queue()
    expected.paused = True
    assert load_prompt_queue_state(active).prompts == [first, expected, later]
    persisted = load_prompt_queue_snapshot(active.store.directory, active.id)
    assert persisted.prompts[1].attachments == selected.attachments
    assert persisted.prompts[1].user_message == selected.user_message
    assert active.store.load(active.id).messages == []
    assert agent.seen_history_lengths == []
    root.mkdir()
    assert dequeue_prompt(state=state, state_lock=lock, active_session=active) is None


@pytest.mark.parametrize("with_image", [False, True])
def test_direct_submission_deleted_after_start_retains_durable_text_and_images(
    tmp_path: Path, with_image: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    state, lock, agent = _state(), Lock(), FakeAgent()
    image = _tiny_png(tmp_path / "image.png")
    if with_image:
        _attach(active, state, image)
    submitted = []

    def start(prompt, *, user_message):
        submitted.append((prompt, user_message))
        return Thread()

    submit_prompt_toolkit_prompt(
        "my draft",
        action="queue",
        state=state,
        state_lock=lock,
        active_session=active,
        start_turn=start,
        steer_active_turn=Mock(),
        invalidate_prompt=lambda: None,
    )
    assert state.pending_images == []
    assert load_prompt_queue_state(active).pending_images == []
    root.rmdir()
    image.path.unlink()
    prompt, message = submitted[0]
    _run_and_finish(active, state, lock, agent, prompt, message)
    restored = load_prompt_queue_state(active).prompts
    assert len(restored) == 1
    assert restored[0].paused
    assert restored[0].prompt == "my draft"
    assert restored[0].user_message == message
    assert restored[0].user_message is not None
    assert restored[0].user_message.has_image_inputs() is with_image
    assert not agent.seen_history_lengths


@pytest.mark.parametrize("checkpoint", [False, True])
def test_workspace_error_after_execution_does_not_restore_duplicate(
    tmp_path: Path, monkeypatch, checkpoint: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    state, lock, agent = _state(), Lock(), FakeAgent()
    pending = PendingPrompt(
        "already executed", user_message=Message.user("already executed")
    )
    append_prompt(state=state, state_lock=lock, active_session=active, prompt=pending)
    assert (
        dequeue_prompt(state=state, state_lock=lock, active_session=active) == pending
    )
    state.active_user_message = pending.user_message

    def execute(*args, **kwargs):
        result = agent.run(pending.prompt, [])
        if checkpoint:
            kwargs["after_tool_result_appended"](
                result.messages, conversation_entries_from_messages(result.messages)
            )
        root.rmdir()
        require_workspace(root, session_id=active.id)

    monkeypatch.setattr(turns, "execute_turn", execute)
    monkeypatch.setattr(turns, "start_session_title_generation", Mock())
    outcome = _run_and_finish(
        active, state, lock, agent, pending.prompt, pending.user_message
    )
    assert outcome.rejected_prompt is None
    assert load_prompt_queue_state(active).prompts == []
    assert state.next_editor_text is None
    assert agent.seen_history_lengths == [0]
    if checkpoint:
        assert [
            message
            for message in active.store.load(active.id).messages
            if message.role == "user"
        ] == [pending.user_message]


def test_control_transfers_queue_identity_before_worker_starts(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    state, lock, agent = _state(), Lock(), FakeAgent()
    pending = PendingPrompt("queued", kind="steering", id="original")
    append_prompt(state=state, state_lock=lock, active_session=active, prompt=pending)
    selected = dequeue_prompt(state=state, state_lock=lock, active_session=active)
    targets = []

    def thread(*, target, **kwargs):
        targets.append(target)
        return Mock()

    monkeypatch.setattr("yoke.cli.interactive.prompt.control.Thread", thread)
    control = create_prompt_toolkit_control(
        state=state,
        state_lock=lock,
        agent=agent,
        active_session_ref={"active_session": active},
        renderer=Mock(),
        scrollback=Mock(),
        request_context_usage=Mock(),
        invalidate_prompt=Mock(),
        update_status=Mock(),
    )
    control.start_pending_prompt(selected, False)
    assert state.starting_prompt is None
    root.rmdir()
    targets[0]()
    restored = load_prompt_queue_state(active).prompts
    assert len(restored) == 1
    assert restored[0].id == "original"
    assert restored[0].kind == "steering"
    assert restored[0].paused
