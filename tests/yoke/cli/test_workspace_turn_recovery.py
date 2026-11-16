"""Deletion races retain daemon input identity and durable image references."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

from pathlib import Path
from threading import Lock, Thread
from unittest.mock import Mock

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.cli.backend.daemon.images import upload_message_images
from yoke.cli.backend.daemon.turn import DaemonSubmittedTurn
from yoke.cli.interactive.common import PendingPrompt, PromptCliState, TurnFailure
from yoke.cli.interactive.prompt import turns
from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.backend.values import ActiveSessionRef
from yoke.session.workspace import require_workspace

from .terminal_daemon_support import ReplyProvider, session_for, terminal as terminal
from .test_queue_action_atomicity import _attach, _tiny_png


def _finish(backend, state, turn):
    list(turn)
    outcome = turn.result()
    assert isinstance(outcome, TurnFailure)
    assert "--relocate" in str(outcome.error)
    assert not turns.handle_prompt_turn_outcome(
        turn_id=state.active_turn_id,
        outcome=outcome,
        state=state,
        state_lock=Lock(),
        agent=backend,
        active_session=session_for(backend),
        renderer=Mock(),
        scrollback=Mock(),
    )
    next_prompt, finish = turns.finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=session_for(backend),
        request_context_usage=lambda _: None,
    )
    assert next_prompt is None
    assert not finish
    assert state.active_user_message is None
    return outcome


def _replay(backend, item):
    stream, unsubscribe = backend.events.subscribe()
    turn = DaemonSubmittedTurn(backend.connection, item.id, stream, unsubscribe)
    backend.connection.session_request(
        "POST",
        "/prompt",
        json={
            "id": item.id,
            "clientID": backend.connection.owner,
            "delivery": "steer" if item.kind == "steering" else "queue",
            "prompt": {
                "text": item.prompt,
                "attachments": item.attachments,
                "continuation": item.continuation,
            },
        },
    )
    return turn


@pytest.mark.parametrize("kind", ["queued", "steering"])
def test_dequeue_delete_run_outcome_finish_restores_exact_paused_item(
    tmp_path: Path, monkeypatch, terminal, kind
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    calls = []

    class Provider(ReplyProvider):
        def complete(self, messages, tools):
            calls.append(messages)
            return super().complete(messages, tools)

    terminal.factory = lambda _: RuntimeAgent(Provider(), [], tool_root=root)
    terminal.store.save("recover", [], root=root, title="Fixture")
    backend = terminal.backend("recover", root=root)
    image = _tiny_png(tmp_path / "image.png")
    original_bytes = image.path.read_bytes()
    from yoke.cli.image_input import build_user_message

    selected = PendingPrompt(
        "describe it",
        kind=kind,
        user_message=build_user_message("describe it", image_paths=[image.path]),
    )
    first, later = (
        PendingPrompt("earlier", paused=True),
        PendingPrompt("later", paused=True),
    )
    backend.commit_queue([first, selected, later], [], expected_revision=0)
    selected_remote = backend.load_queue().prompts[1]
    runtime = terminal.app.state.runtime_registry.get_if_loaded("recover")
    original = runtime.execution.prepare

    def prepare(*args, **kwargs):
        # The daemon has promoted this exact identity before preparation begins.
        assert [p.id for p in backend.load_queue().prompts] == [first.id, later.id]
        root.rmdir()
        image.path.unlink()
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.execution, "prepare", prepare)
    outcome = _finish(
        backend,
        PromptCliState(messages=[], pending_prompts=[], active_turn_id=1),
        _replay(backend, selected_remote),
    )
    assert not hasattr(outcome, "agent")
    restored = backend.load_queue().prompts
    assert [(p.id, p.prompt, p.kind, p.paused) for p in restored] == [
        (first.id, first.prompt, first.kind, True),
        (selected.id, selected.prompt, kind, True),
        (later.id, later.prompt, later.kind, True),
    ]
    assert restored[1].created_at == selected_remote.created_at
    attachment = restored[1].attachments[0]
    assert attachment["imageProjection"] == "cli"
    path = terminal.app.state.upload_service.resolve(
        attachment["uri"],
        session_id="recover",
        name=attachment["name"],
        mime=attachment["mime"],
    )
    assert path.read_bytes() == original_bytes
    assert terminal.store.load("recover").messages == []
    assert calls == []
    root.mkdir()
    from .daemon_queue_support import wake

    wake(session_for(backend))
    assert [p.id for p in backend.load_queue().prompts] == [
        first.id,
        selected.id,
        later.id,
    ]
    assert calls == []


@pytest.mark.parametrize("with_image", [False, True])
def test_direct_submission_deleted_after_start_retains_durable_text_and_images(
    tmp_path: Path, monkeypatch, terminal, with_image: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    calls = []

    class Provider(ReplyProvider):
        def complete(self, messages, tools):
            calls.append(messages)
            return super().complete(messages, tools)

    terminal.factory = lambda _: RuntimeAgent(Provider(), [], tool_root=root)
    terminal.store.save("recover", [], root=root, title="Fixture")
    backend = terminal.backend("recover", root=root)
    active = session_for(backend)
    state = PromptCliState(messages=[], pending_prompts=[], active_turn_id=1)
    image = _tiny_png(tmp_path / "image.png")
    original_bytes = image.path.read_bytes()
    if with_image:
        _attach(active, state, image)
    submitted = []

    def start(prompt, *, user_message):
        submitted.append(user_message)
        backend.connection.session_request(
            "POST",
            "/prompt",
            json={
                "id": "direct",
                "clientID": backend.connection.owner,
                "resume": False,
                "delivery": "queue",
                "prompt": {
                    "text": prompt,
                    "attachments": upload_message_images(
                        backend.connection, user_message
                    ),
                },
            },
        )
        return Thread()

    submit_prompt_toolkit_prompt(
        "my draft",
        action="queue",
        state=state,
        state_lock=Lock(),
        active_session=active,
        start_turn=start,
        steer_active_turn=Mock(),
        invalidate_prompt=lambda: None,
    )
    assert state.pending_images == []
    assert backend.load_queue().pending_images == []
    assert len(submitted) == 1
    runtime = terminal.app.state.runtime_registry.get_if_loaded("recover")
    original = runtime.execution.prepare

    def prepare(*args, **kwargs):
        root.rmdir()
        image.path.unlink()
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.execution, "prepare", prepare)
    _finish(backend, state, _replay(backend, backend.load_queue().prompts[0]))
    restored = backend.load_queue().prompts
    assert len(restored) == 1
    assert (restored[0].id, restored[0].paused, restored[0].prompt) == (
        "direct",
        True,
        "my draft",
    )
    assert bool(restored[0].attachments) is with_image
    assert submitted[0].has_image_inputs() is with_image
    if with_image:
        attachment = restored[0].attachments[0]
        path = terminal.app.state.upload_service.resolve(
            attachment["uri"],
            session_id="recover",
            name=attachment["name"],
            mime=attachment["mime"],
        )
        assert path.read_bytes() == original_bytes
    assert calls == []


@pytest.mark.parametrize("checkpoint", [False, True])
def test_workspace_error_after_execution_does_not_restore_duplicate(
    tmp_path: Path, monkeypatch, terminal, checkpoint: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    terminal.store.save("recover", [], root=root, title="Fixture")
    backend = terminal.backend("recover", root=root)
    pending = PendingPrompt(
        "already executed", user_message=Message.user("already executed")
    )
    backend.commit_queue([pending], [], expected_revision=0)
    executed = []
    original_run = RuntimeAgent.run

    def run(self, *args, **kwargs):
        assert self.messages == []
        result = original_run(self, *args, **kwargs)
        executed.append(result)
        if checkpoint:
            kwargs["after_tool_result_appended"](
                self.context_for_messages(result.messages)
            )
        root.rmdir()
        require_workspace(root, session_id="recover")

    monkeypatch.setattr(RuntimeAgent, "run", run)
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        active_turn_id=1,
        active_user_message=pending.user_message,
    )
    outcome = _finish(backend, state, _replay(backend, backend.load_queue().prompts[0]))
    assert not hasattr(outcome, "rejected_prompt")
    assert backend.load_queue().prompts == []
    assert state.next_editor_text is None
    assert len(executed) == 1
    if checkpoint:
        assert [
            m for m in terminal.store.load("recover").messages if m.role == "user"
        ] == [pending.user_message]


def test_control_transfers_queue_identity_before_worker_starts(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    backend = terminal.backend("recover", root=root)
    active = session_for(backend)
    state = PromptCliState(messages=[], pending_prompts=[])
    pending = PendingPrompt("queued", kind="steering", id="original")
    backend.commit_queue([pending], [], expected_revision=0)
    targets = []

    def thread(*, target, **kwargs):
        targets.append(target)
        return Mock()

    monkeypatch.setattr("yoke.cli.interactive.prompt.control.Thread", thread)
    control = create_prompt_toolkit_control(
        state=state,
        state_lock=Lock(),
        agent=backend,
        active_session_ref=ActiveSessionRef(active),
        renderer=Mock(),
        scrollback=Mock(),
        request_context_usage=Mock(),
        invalidate_prompt=Mock(),
        update_status=Mock(),
    )
    # The daemon queue is authoritative even if a stale UI callback still holds it.
    from yoke.cli.interactive.queue.mutations import dequeue_prompt

    selected = dequeue_prompt(state=state, state_lock=Lock(), active_session=active)
    assert selected is None
    control.start_pending_prompt(selected, False)
    assert not hasattr(state, "starting_prompt")
    runtime = terminal.app.state.runtime_registry.get_if_loaded("recover")
    original = runtime.execution.prepare

    def prepare(*args, **kwargs):
        root.rmdir()
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.execution, "prepare", prepare)
    _finish(backend, state, _replay(backend, backend.load_queue().prompts[0]))
    assert targets == []
    restored = backend.load_queue().prompts
    assert len(restored) == 1
    assert restored[0].id == "original"
    assert restored[0].kind == "steering"
    assert restored[0].paused
    assert terminal.store.load("recover").messages == []
