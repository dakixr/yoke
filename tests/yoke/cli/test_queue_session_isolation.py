from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from threading import Lock, Thread

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.prompt.loop import process_prompt_toolkit_prompt
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.cli.interactive.queue.persistence import persist_prompt_queue
from yoke.cli.render import build_console
from yoke.session.queue import load_prompt_queue_snapshot

from .support import CaptureStream, FakeAgent, active_session_for


def test_new_session_does_not_inherit_previous_session_queue(tmp_path: Path) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("old session item", id="old-item", paused=True)],
    )
    loaded = load_prompt_queue_state(active_session)
    state = PromptCliState(
        messages=[],
        pending_prompts=loaded.prompts,
        pending_images=loaded.pending_images,
        queue_revision=loaded.revision,
        queue_session_id=active_session.id,
    )
    active_session_ref = {"active_session": active_session}

    new_session = process_prompt_toolkit_prompt(
        "/new",
        state=state,
        agent=FakeAgent(),
        active_session_ref=active_session_ref,
        scrollback_console=build_console(CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
    )

    assert new_session.id != active_session.id
    assert active_session_ref["active_session"].id == new_session.id
    assert state.pending_prompts == []
    assert state.pending_images == []
    assert state.queue_revision == 0
    assert state.queue_session_id == new_session.id
    old_queue = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    new_queue = load_prompt_queue_snapshot(
        new_session.store.directory,
        new_session.id,
    )
    assert [item.prompt for item in old_queue.prompts] == ["old session item"]
    assert new_queue.prompts == []


def test_idle_fork_switches_messages_session_and_queue_mirror(tmp_path: Path) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("source-only item", id="source-item", paused=True)],
    )
    loaded = load_prompt_queue_state(active_session)
    state = PromptCliState(
        messages=[],
        pending_prompts=loaded.prompts,
        pending_images=loaded.pending_images,
        queue_revision=loaded.revision,
        queue_session_id=active_session.id,
    )
    active_session_ref = {"active_session": active_session}

    forked = process_prompt_toolkit_prompt(
        "/fork",
        state=state,
        agent=FakeAgent(),
        active_session_ref=active_session_ref,
        scrollback_console=build_console(CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
    )

    assert forked.id != active_session.id
    assert active_session_ref["active_session"] is forked
    assert state.messages == forked.messages()
    assert state.pending_prompts == []
    assert state.pending_images == []
    assert state.queue_revision == 0
    assert state.queue_session_id == forked.id
    source_queue = load_prompt_queue_snapshot(
        active_session.store.directory,
        active_session.id,
    )
    assert [item.id for item in source_queue.prompts] == ["source-item"]
