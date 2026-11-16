"""Install backend queue snapshots into prompt-toolkit UI state."""

from __future__ import annotations

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.runtime import ActiveSession
from yoke.cli.backend.queue import load_prompt_queue_state
from yoke.cli.backend.values import LoadedPromptQueue


def queue_ensure_session(state: PromptCliState, active_session: ActiveSession) -> None:
    if state.queue_session_id is None:
        state.queue_session_id = active_session.id
    elif state.queue_session_id != active_session.id:
        queue_reload(state, active_session)


def queue_reload(state: PromptCliState, active_session: ActiveSession) -> None:
    queue_install_loaded(state, active_session, load_prompt_queue_state(active_session))


def queue_refresh_if_changed(
    state: PromptCliState,
    active_session: ActiveSession,
) -> None:
    loaded = load_prompt_queue_state(active_session)
    if loaded.revision != state.queue_revision:
        queue_install_loaded(state, active_session, loaded)


def queue_install_loaded(
    state: PromptCliState,
    active_session: ActiveSession,
    loaded: LoadedPromptQueue,
) -> None:
    state.pending_prompts = loaded.prompts
    state.pending_images = loaded.pending_images
    state.queue_revision = loaded.revision
    state.queue_session_id = active_session.id
