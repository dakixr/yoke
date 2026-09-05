"""Prompt submission helpers for the prompt-toolkit interactive loop."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from threading import Thread

from yoke.cli.image_input import ImageAttachment, build_user_message
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.queue.mutations import consume_pending_images
from yoke.cli.interactive.queue.mutations import observe_prompt_submission
from yoke.cli.interactive.queue.mutations import queue_prompt_submission
from yoke.cli.runtime import ActiveSession


def submit_prompt_toolkit_prompt(
    prompt: str,
    *,
    action: str,
    state: PromptCliState,
    active_session: ActiveSession,
    state_lock: Lock,
    invalidate_prompt: Callable[[], None],
    start_turn: Callable[..., Thread],
    steer_active_turn: Callable[..., bool],
) -> None:
    """Submit a normal prompt without reprocessing slash commands."""
    try:
        pending_images, _ = observe_prompt_submission(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
        )
        user_message = build_user_message(
            prompt,
            image_paths=[image.path for image in pending_images],
        )
        _, idle = observe_prompt_submission(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
        )
    except Exception:
        _restore_prompt_text(state, state_lock, prompt)
        raise
    if idle:
        try:
            start_turn(prompt, user_message=user_message)
        except Exception:
            _restore_prompt_text(state, state_lock, prompt)
            raise
        consume_pending_images(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
            images=pending_images,
        )
        return
    queued = PendingPrompt(
        prompt,
        user_message=user_message,
        kind="queued",
    )
    if action == "queue":
        _queue_or_restore(
            queued,
            pending_images=pending_images,
            state=state,
            state_lock=state_lock,
            active_session=active_session,
        )
        invalidate_prompt()
        return
    try:
        steered = steer_active_turn(prompt, user_message=user_message)
    except Exception:
        _restore_prompt_text(state, state_lock, prompt)
        raise
    if steered:
        consume_pending_images(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
            images=pending_images,
        )
        return
    _queue_or_restore(
        queued,
        pending_images=pending_images,
        state=state,
        state_lock=state_lock,
        active_session=active_session,
    )
    invalidate_prompt()


def _queue_or_restore(
    prompt: PendingPrompt,
    *,
    pending_images: list[ImageAttachment],
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
) -> None:
    try:
        queued = queue_prompt_submission(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
            prompt=prompt,
            consumed_images=pending_images,
        )
    except Exception:
        _restore_prompt_text(state, state_lock, prompt.prompt)
        raise
    if not queued:
        _restore_prompt_text(state, state_lock, prompt.prompt)


def _restore_prompt_text(state: PromptCliState, state_lock: Lock, prompt: str) -> None:
    with state_lock:
        state.next_editor_text = prompt
