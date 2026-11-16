from __future__ import annotations

# ruff: noqa: D100,D103,S101
from pathlib import Path
from threading import Lock, Thread

from yoke.cli.interactive.common import (
    PendingPrompt,
    PromptCliState,
    format_context_usage_text,
)
from yoke.cli.interactive.prompt.loop import process_prompt_toolkit_prompt
from yoke.cli.backend.queue import load_prompt_queue_state, persist_prompt_queue
from yoke.cli.render import build_console
from yoke.cli.runtime.base import ActiveSessionRef
from yoke.session.queue import load_prompt_queue_snapshot

from .support import CaptureStream
from .terminal_daemon_support import session_for, terminal as terminal


def test_new_session_does_not_inherit_previous_session_queue(
    tmp_path: Path, terminal
) -> None:
    backend = terminal.backend("source", root=tmp_path)
    active_session = session_for(backend)
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
    active_session_ref = ActiveSessionRef(active_session)

    new_session = process_prompt_toolkit_prompt(
        "/new",
        state=state,
        agent=backend,
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
    assert active_session_ref.value.id == new_session.id
    assert state.pending_prompts == []
    assert state.pending_images == []
    assert state.queue_revision == 0
    assert state.queue_session_id == new_session.id
    old_queue = load_prompt_queue_snapshot(
        terminal.store.directory,
        active_session.id,
    )
    new_queue = load_prompt_queue_snapshot(
        terminal.store.directory,
        new_session.id,
    )
    assert [item.prompt for item in old_queue.prompts] == ["old session item"]
    assert new_queue.prompts == []


def test_idle_fork_switches_messages_session_and_queue_mirror(
    tmp_path: Path, terminal
) -> None:
    backend = terminal.backend("source", root=tmp_path)
    active_session = session_for(backend)
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
    active_session_ref = ActiveSessionRef(active_session)

    forked = process_prompt_toolkit_prompt(
        "/fork",
        state=state,
        agent=backend,
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
    assert active_session_ref.value is forked
    assert state.messages == forked.messages()
    assert state.pending_prompts == []
    assert state.pending_images == []
    assert state.queue_revision == 0
    assert state.queue_session_id == forked.id
    source_queue = load_prompt_queue_snapshot(
        terminal.store.directory,
        active_session.id,
    )
    assert [item.id for item in source_queue.prompts] == ["source-item"]
