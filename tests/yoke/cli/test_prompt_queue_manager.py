from __future__ import annotations

# ruff: noqa: ANN002,ANN003,ANN202,D100,D103,S101
from pathlib import Path
from threading import Lock

import pytest

from yoke.cli.interactive.common import (
    PendingPrompt,
    PromptCliState,
    format_context_usage_text,
)
from yoke.cli.interactive.prompt.loop import process_prompt_toolkit_prompt
from yoke.cli.interactive.queue import manager
from yoke.cli.backend.queue import load_prompt_queue_state, persist_prompt_queue
from yoke.cli.render import build_console
from yoke.cli.runtime.base import ActiveSessionRef
from yoke.cli.runtime.terminal_output_gate import (
    is_fullscreen_output_suppressed,
)
from yoke.session.queue import load_prompt_queue_snapshot

from .support import CaptureStream
from .terminal_daemon_support import session_for, terminal as terminal


def test_queue_manager_defers_turn_output_across_item_edit(
    monkeypatch,
) -> None:
    """Live turn output must not overwrite the queue item editor."""
    manager_runs = 0

    def run_manager(state, *, prompts, changed):
        nonlocal manager_runs
        del prompts, changed
        manager_runs += 1
        assert is_fullscreen_output_suppressed()
        if manager_runs == 1:
            return manager._QueueManagerEditRequest(0)
        return state.prompts

    def edit_prompt(prompt: PendingPrompt) -> PendingPrompt:
        assert is_fullscreen_output_suppressed()
        edited = prompt.copy_for_queue()
        edited.prompt = "edited"
        return edited

    monkeypatch.setattr(manager, "_run_queue_manager", run_manager)

    result = manager.open_queue_manager(
        [PendingPrompt("original")], edit_prompt=edit_prompt
    )

    assert result is not None
    assert [prompt.prompt for prompt in result] == ["edited"]
    assert manager_runs == 2
    assert not is_fullscreen_output_suppressed()


def test_queue_editor_defers_turn_output_when_used_directly(
    monkeypatch,
) -> None:
    """The standalone queue editor must also own terminal output."""

    class FakePromptSession:
        def __init__(self, *, multiline: bool) -> None:
            assert multiline

        def prompt(self, *_args, **_kwargs) -> str:
            assert is_fullscreen_output_suppressed()
            return "edited"

    import prompt_toolkit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)

    result = manager.edit_queue_prompt(PendingPrompt("original"))

    assert result is not None
    assert result.prompt == "edited"
    assert not is_fullscreen_output_suppressed()


def test_queue_manager_rejects_save_when_queue_changed_while_open(
    tmp_path: Path,
    monkeypatch,
    terminal,
) -> None:
    backend = terminal.backend("queue-manager", root=tmp_path)
    other = terminal.backend("queue-manager", root=tmp_path)
    active_session = session_for(backend)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("original", id="item-a", paused=True)],
    )
    loaded = load_prompt_queue_state(active_session)
    state = PromptCliState(
        messages=[],
        pending_prompts=loaded.prompts,
        pending_images=loaded.pending_images,
        queue_revision=loaded.revision,
        queue_session_id=active_session.id,
    )
    stdout = CaptureStream()

    def change_disk_then_save(manager_state, *, prompts, changed):
        del prompts, changed
        latest = other.load_queue()
        latest.prompts[0].prompt = "authoritative edit"
        other.commit_queue(latest.prompts, [], expected_revision=latest.revision)
        manager_state.prompts[0].prompt = "manager edit"
        return manager_state.prompts

    monkeypatch.setattr(manager, "_run_queue_manager", change_disk_then_save)

    process_prompt_toolkit_prompt(
        "/queue",
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(active_session),
        scrollback_console=build_console(stdout),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: pytest.fail("stale item started"),
        start_pending_prompt=lambda *_args, **_kwargs: pytest.fail(
            "stale item started"
        ),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
    )

    current = load_prompt_queue_snapshot(
        terminal.store.directory,
        active_session.id,
    )
    assert current.revision == loaded.revision + 1
    assert [item.prompt for item in current.prompts] == ["authoritative edit"]
    assert [prompt.prompt for prompt in state.pending_prompts] == ["authoritative edit"]
    assert state.queue_revision == current.revision
    assert "Queue changed while the manager was open" in stdout.getvalue()
